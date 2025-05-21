import os, json
from typing import Any

from cerebrum.client import Cerebrum
from cerebrum import config
from cerebrum.interface import AutoTool

class BaseAgent:
    def __init__(self, agent_name, task_input, config_):
        self.agent_name = agent_name
        self.task_input = task_input
        self.config = config_
        
        print("Debug - BaseAgent init:")
        print(f"config_: {config_}")
        print(f"self.config: {self.config}")
        

        if not isinstance(self.config, dict):
            raise ValueError(f"Config must be a dict, got {type(self.config)}")
        if "tools" not in self.config:
            raise ValueError(f"Config must contain 'tools' key. Current config: {self.config}")
        
        config.global_client = Cerebrum()
        # self.send_request = AutoLLM.from_dynamic().process
        self.send_request = None

        self.tools, self.tool_info = AutoTool.from_batch_preload(self.config["tools"]).values()


    # def _load_config(self, dir: str):
    #     # script_path = os.path.abspath(__file__)
    #     # script_dir = os.path.dirname(script_path)
    #     # print('script dir', script_dir)
    #     # config_file = os.path.join(script_dir, "config.json")
    #     config_file = os.path.join(dir, "config.json")

        # with open(config_file, "r") as f:
        #     config = json.load(f)
        #     return config
        
    def pre_select_tools(self, tool_names):
        pre_selected_tools = []
        for tool_name in tool_names:
            for tool in self.tools:
                if tool["function"]["name"] == tool_name:
                    pre_selected_tools.append(tool)
                    break

        return pre_selected_tools
    
    def run(self) -> Any:
        return {}