call Scripts\activate.bat & python -c "import logging; logging.basicConfig(level=logging.DEBUG); import mpm.api; mpm.api.enable_plugin('droplet_planning_plugin'); mpm.api.enable_plugin('dmf_device_ui_plugin'); mpm.api.enable_plugin('dropbot_plugin');mpm.api.enable_plugin('user_prompt_plugin'); mpm.api.enable_plugin('step_label_plugin')"
if errorlevel 1 exit 1