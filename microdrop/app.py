from argparse import ArgumentParser
import logging
import os
try:
    import cPickle as pickle
except ImportError:
    import pickle
import pprint
import re
import sys
import threading

from flatland import Integer, Form, String, Enum, Boolean
from pygtkhelpers.ui.extra_widgets import Filepath
from pygtkhelpers.ui.form_view_dialog import FormViewDialog
import gtk
import mpm.api
import path_helpers as ph

from . import base_path, MICRODROP_PARSER
from . import plugin_manager, __version__
from .config import Config
from .gui.dmf_device_controller import DEVICE_FILENAME
from .logging_helpers import _L, caller_name  #: .. versionadded:: X.X.X
from .logger import CustomHandler
from .plugin_helpers import AppDataController
from .plugin_manager import (ExtensionPoint, IPlugin, SingletonPlugin,
                             implements, PluginGlobals)
from .protocol import Step


logger = logging.getLogger(__name__)

PluginGlobals.push_env('microdrop')


def parse_args(args=None):
    """Parses arguments, returns (options, args)."""
    if args is None:
        args = sys.argv[1:]

    parser = ArgumentParser(parents=[MICRODROP_PARSER])
    args = parser.parse_args(args)
    return args


def test(*args, **kwargs):
    print 'args=%s\nkwargs=%s' % (args, kwargs)


class App(SingletonPlugin, AppDataController):
    implements(IPlugin)
    '''
INFO:  <Plugin App 'microdrop.app'>
INFO:  <Plugin ConfigController 'microdrop.gui.config_controller'>
INFO:  <Plugin DmfDeviceController 'microdrop.gui.dmf_device_controller'>
INFO:  <Plugin ExperimentLogController
         'microdrop.gui.experiment_log_controller'>
INFO:  <Plugin MainWindowController 'microdrop.gui.main_window_controller'>
INFO:  <Plugin ProtocolController 'microdrop.gui.protocol_controller'>
INFO:  <Plugin ProtocolGridController 'microdrop.gui.protocol_grid_controller'>
    '''
    core_plugins = ['microdrop.app', 'microdrop.gui.config_controller',
                    'microdrop.gui.dmf_device_controller',
                    'microdrop.gui.experiment_log_controller',
                    'microdrop.gui.main_window_controller',
                    'microdrop.gui.protocol_controller',
                    'microdrop.gui.protocol_grid_controller',
                    'microdrop.zmq_hub_plugin',
                    'microdrop.electrode_controller_plugin',
                    'microdrop.device_info_plugin']

    AppFields = Form.of(
        Integer.named('x').using(default=None, optional=True,
                                 properties={'show_in_gui': False}),
        Integer.named('y').using(default=None, optional=True,
                                 properties={'show_in_gui': False}),
        Integer.named('width').using(default=400, optional=True,
                                     properties={'show_in_gui': False}),
        Integer.named('height').using(default=500, optional=True,
                                      properties={'show_in_gui': False}),
        String.named('server_url')
        .using(default='http://microfluidics.utoronto.ca/update',
               optional=True, properties=dict(show_in_gui=False)),
        Boolean.named('realtime_mode')
        .using(default=False, optional=True,
               properties=dict(show_in_gui=False)),
        Filepath.named('log_file')
        .using(default='', optional=True,
               properties={'action': gtk.FILE_CHOOSER_ACTION_SAVE}),
        Boolean.named('log_enabled').using(default=False, optional=True),
        Enum.named('log_level').using(default='info', optional=True)
        .valued('debug', 'info', 'warning', 'error', 'critical'))

    def __init__(self):
        '''
        .. versionchanged:: 2.11.2
            Add :attr:`gtk_thread` attribute, holding a reference to the thread
            that the GTK main loop is executing in.

        .. versionchanged:: 2.17
            Remove :attr:`version` attribute.  Use
            :attr:`microdrop.__version__` instead.
        '''
        args = parse_args()

        print 'Arguments: %s' % args

        self.name = "microdrop.app"
        #: .. versionadded:: 2.11.2
        self.gtk_thread = None

        self.realtime_mode = False
        self.running = False
        self.builder = gtk.Builder()
        self.signals = {}
        self.plugin_data = {}

        # these members are initialized by plugins
        self.experiment_log_controller = None
        self.config_controller = None
        self.dmf_device_controller = None
        self.protocol_controller = None
        self.main_window_controller = None

        # Enable custom logging handler
        logging.getLogger().addHandler(CustomHandler())
        self.log_file_handler = None

        # config model
        try:
            self.config = Config(args.config)
        except IOError:
            logging.error('Could not read configuration file, `%s`.  Make sure'
                          ' it exists and is readable.', args.config)
            raise SystemExit(-1)

        # set the log level
        if self.name in self.config.data and ('log_level' in
                                              self.config.data[self.name]):
            self._set_log_level(self.config.data[self.name]['log_level'])
        _L().info('MicroDrop version: %s', __version__)
        _L().info('Running in working directory: %s', os.getcwd())

        # dmf device
        self.dmf_device = None

        # protocol
        self.protocol = None

    def get_data(self, plugin_name):
        data = self.plugin_data.get(plugin_name)
        if data:
            return data
        else:
            return {}

    def set_data(self, plugin_name, data):
        '''
        .. versionchanged:: X.X.X
            Log data and plugin name to debug level.
        '''
        logger = _L()  # use logger with method context
        if logger.getEffectiveLevel() >= logging.DEBUG:
            caller = caller_name(skip=2)
            logger.debug('%s -> plugin_data:', caller)
            map(logger.debug, pprint.pformat(data).splitlines())
        self.plugin_data[plugin_name] = data

    def on_app_options_changed(self, plugin_name):
        if plugin_name == self.name:
            data = self.get_data(self.name)
            if 'realtime_mode' in data:
                if self.realtime_mode != data['realtime_mode']:
                    self.realtime_mode = data['realtime_mode']
                    if self.protocol_controller:
                        self.protocol_controller.run_step()
            if 'log_file' in data and 'log_enabled' in data:
                self.apply_log_file_config(data['log_file'],
                                           data['log_enabled'])
            if 'log_level' in data:
                self._set_log_level(data['log_level'])
            if 'width' in data and 'height' in data:
                self.main_window_controller.view.resize(data['width'],
                                                        data['height'])
                # allow window to resize before other signals are processed
                while gtk.events_pending():
                    gtk.main_iteration()
            if data.get('x') is not None and data.get('y') is not None:
                self.main_window_controller.view.move(data['x'], data['y'])
                # allow window to resize before other signals are processed
                while gtk.events_pending():
                    gtk.main_iteration()

    def apply_log_file_config(self, log_file, enabled):
        if enabled and not log_file:
            _L().error('Log file can only be enabled if a path is selected.')
            return False
        self.update_log_file()
        return True

    @property
    def plugins(self):
        return set(self.plugin_data.keys())

    def plugin_name_lookup(self, name, re_pattern=False):
        if not re_pattern:
            return name

        for plugin_name in self.plugins:
            if re.search(name, plugin_name):
                return plugin_name
        return None

    def update_plugins(self):
        '''
        .. versionchanged:: 2.16.2
            Method was deprecated.
        '''
        raise DeprecationWarning('The `update_plugins` method was deprecated '
                                 'in version 2.16.2.')

    def gtk_thread_active(self):
        '''
        Returns
        -------
        bool
            ``True`` if the currently active thread is the GTK thread.

        .. versionadded:: 2.11.2
        '''
        if self.gtk_thread is not None and (threading.current_thread().ident ==
                                            self.gtk_thread.ident):
            return True
        else:
            return False

    def run(self):
        '''
        .. versionchanged:: 2.11.2
            Set :attr:`gtk_thread` attribute, holding a reference to the thread
            that the GTK main loop is executing in.

        .. versionchanged:: 2.16.2
            Do not attempt to update plugins.
        '''
        logger = _L()  # use logger with method context
        self.gtk_thread = threading.current_thread()

        # set realtime mode to false on startup
        if self.name in self.config.data and \
                'realtime_mode' in self.config.data[self.name]:
            self.config.data[self.name]['realtime_mode'] = False

        plugin_manager.emit_signal('on_plugin_enable')
        log_file = self.get_app_values()['log_file']
        if not log_file:
            self.set_app_values({'log_file':
                                 ph.path(self.config['data_dir'])
                                 .joinpath('microdrop.log')})

        pwd = ph.path(os.getcwd()).realpath()
        if '' in sys.path and pwd.joinpath('plugins').isdir():
            logger.info('[warning] Removing working directory `%s` from Python'
                        ' import path.', pwd)
            sys.path.remove('')

        # Import enabled plugins from Conda environment.
        conda_plugins_dir = mpm.api.MICRODROP_CONDA_ETC.joinpath('plugins',
                                                                 'enabled')
        if conda_plugins_dir.isdir():
            plugin_manager.load_plugins(conda_plugins_dir,
                                        import_from_parent=False)
        self.update_log_file()

        logger.info('User data directory: %s', self.config['data_dir'])
        logger.info('Plugins directory: %s', conda_plugins_dir)
        logger.info('Devices directory: %s', self.get_device_directory())

        FormViewDialog.default_parent = self.main_window_controller.view
        self.builder.connect_signals(self.signals)

        observers = {}
        plugins_to_disable_by_default = []
        # Enable plugins according to schedule requests
        for package_name in self.config['plugins']['enabled']:
            try:
                service = plugin_manager. \
                    get_service_instance_by_package_name(package_name)
                observers[service.name] = service
            except KeyError:
                logger.warning('No plugin found registered with name `%s`',
                               package_name)
                # Mark plugin to be removed from "enabled" list to prevent
                # trying to enable it on future launches.
                plugins_to_disable_by_default.append(package_name)
            except Exception, exception:
                logger.error(exception, exc_info=True)
        # Remove marked plugins from "enabled" list to prevent trying to enable
        # it on future launches.
        for package_name_i in plugins_to_disable_by_default:
            self.config['plugins']['enabled'].remove(package_name_i)

        schedule = plugin_manager.get_schedule(observers, "on_plugin_enable")

        # Load optional plugins marked as enabled in config
        for p in schedule:
            try:
                plugin_manager.enable(p)
            except KeyError:
                logger.warning('Requested plugin (%s) is not available.\n\n'
                               'Please check that it exists in the plugins '
                               'directory:\n\n    %s' %
                               (p, self.config['plugins']['directory']),
                               exc_info=True)
        plugin_manager.log_summary()

        self.experiment_log = None

        # save the protocol name from the config file because it is
        # automatically overwritten when we load a new device
        protocol_name = self.config['protocol']['name']

        # if there is no device specified in the config file, try choosing one
        # from the device directory by default
        device_directory = ph.path(self.get_device_directory())
        if not self.config['dmf_device']['name']:
            try:
                self.config['dmf_device']['name'] = \
                    device_directory.dirs()[0].name
            except Exception:
                pass

        # load the device from the config file
        if self.config['dmf_device']['name']:
            if device_directory:
                device_path = os.path.join(device_directory,
                                           self.config['dmf_device']['name'],
                                           DEVICE_FILENAME)
                self.dmf_device_controller.load_device(device_path)

        # if we successfully loaded a device
        if self.dmf_device:
            # reapply the protocol name to the config file
            self.config['protocol']['name'] = protocol_name

            # load the protocol
            if self.config['protocol']['name']:
                directory = self.get_device_directory()
                if directory:
                    filename = os.path.join(directory,
                                            self.config['dmf_device']['name'],
                                            "protocols",
                                            self.config['protocol']['name'])
                    self.protocol_controller.load_protocol(filename)

        data = self.get_data("microdrop.app")
        x = data.get('x', None)
        y = data.get('y', None)
        width = data.get('width', 400)
        height = data.get('height', 600)
        self.main_window_controller.view.resize(width, height)
        if x is not None and y is not None:
            self.main_window_controller.view.move(x, y)
        plugin_manager.emit_signal('on_gui_ready')
        self.main_window_controller.main()

    def _set_log_level(self, level):
        '''
        .. versionchanged:: X.X.X
            Set log level on root logger.
        '''
        logging.info('set log level %s', logging.getLevelName(level))
        logging.root.level = getattr(logging, level.upper())

    def _set_log_file_handler(self, log_file):
        '''
        .. versionchanged:: X.X.X
            Add file handler to *root logger* (not *module logger*).  This
            ensures that all logging messages are handled by the file handler.

            Include logger name in message format.
        '''
        if self.log_file_handler:
            self._destroy_log_file_handler()

        try:
            self.log_file_handler = \
                logging.FileHandler(log_file, disable_existing_loggers=False)
        except TypeError:
            # Assume old version of `logging` module without support for
            # `disable_existing_loggers` keyword argument.
            self.log_file_handler = logging.FileHandler(log_file)

        formatter = logging.Formatter('%(asctime)s [%(levelname)s:%(name)s]: '
                                      '%(message)s',
                                      datefmt=r'%Y-%m-%d %H:%M:%S')
        self.log_file_handler.setFormatter(formatter)
        logging.root.addHandler(self.log_file_handler)
        _L().info('added FileHandler: %s (level=%s)', log_file,
                  logging.getLevelName(self.log_file_handler.level))

    def _destroy_log_file_handler(self):
        if self.log_file_handler is None:
            return
        _L().info('closing log_file_handler')
        self.log_file_handler.close()
        del self.log_file_handler
        self.log_file_handler = None

    def update_log_file(self):
        plugin_name = 'microdrop.app'
        values = AppDataController.get_plugin_app_values(plugin_name)
        _L().debug('update_log_file %s', values)
        required = set(['log_enabled', 'log_file'])
        if values is None or required.intersection(values.keys()) != required:
            return
        # values contains both log_enabled and log_file
        log_file = values['log_file']
        log_enabled = values['log_enabled']
        if self.log_file_handler is None:
            if log_enabled:
                self._set_log_file_handler(log_file)
                _L().info('[App] logging enabled')
        else:
            # Log file handler already exists
            if log_enabled:
                if log_file != self.log_file_handler.baseFilename:
                    # Requested log file path has been changed
                    self._set_log_file_handler(log_file)
            else:
                self._destroy_log_file_handler()

    def on_dmf_device_swapped(self, old_dmf_device, dmf_device):
        self.dmf_device = dmf_device

    def on_protocol_swapped(self, old_protocol, new_protocol):
        self.protocol = new_protocol

    def on_experiment_log_changed(self, experiment_log):
        self.experiment_log = experiment_log

    def get_device_directory(self):
        observers = ExtensionPoint(IPlugin)
        plugin_name = 'microdrop.gui.dmf_device_controller'
        service = observers.service(plugin_name)
        values = service.get_app_values()
        if values and 'device_directory' in values:
            directory = ph.path(values['device_directory'])
            if directory.isdir():
                return directory
        return None

    def paste_steps(self, step_number=None):
        if step_number is None:
            # Default to pasting after the current step
            step_number = self.protocol.current_step_number + 1
        clipboard = gtk.clipboard_get()
        try:
            new_steps = pickle.loads(clipboard.wait_for_text())
            for step in new_steps:
                if not isinstance(step, Step):
                    # Invalid object type
                    return
        except (Exception,), why:
            _L().info('invalid data: %s', why)
            return
        self.protocol.insert_steps(step_number, values=new_steps)

    def copy_steps(self, step_ids):
        steps = [self.protocol.steps[id] for id in step_ids]
        if steps:
            clipboard = gtk.clipboard_get()
            clipboard.set_text(pickle.dumps(steps))

    def delete_steps(self, step_ids):
        self.protocol.delete_steps(step_ids)

    def cut_steps(self, step_ids):
        self.copy_steps(step_ids)
        self.delete_steps(step_ids)


PluginGlobals.pop_env()


if __name__ == '__main__':
    os.chdir(base_path())
