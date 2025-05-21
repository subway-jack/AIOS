import asyncio
import inspect
import logging
import uuid
import warnings
from collections import defaultdict
from termcolor import colored
from typing import Union, List, Optional, Callable, Dict, Literal, Any, Tuple

from cerebrum.community.adapter.adapter import FrameworkType
from cerebrum.community.adapter.adapter import add_framework_adapter, get_request_func
from openai import APITimeoutError, APIError

from cerebrum.llm.apis import LLMQuery

try:
    from autogen import (
        OpenAIWrapper,
        ConversableAgent,
        Agent,
        ModelClient,
        Cache
    )
    from autogen.code_utils import (
        content_str,
        decide_use_docker,
        check_can_use_docker_or_throw
    )
    from autogen.runtime_logging import (
        logging_enabled,
        log_new_agent
    )

    from autogen._pydantic import model_dump
    from autogen.coding import CodeExecutorFactory
    from autogen.io import IOStream

    from autogen.logger.logger_utils import get_current_ts
    from autogen.oai.client import LEGACY_DEFAULT_CACHE_SEED, LEGACY_CACHE_DIR, PlaceHolderClient
    from autogen.oai.openai_utils import get_key
    from autogen.runtime_logging import logging_enabled, log_new_wrapper, log_chat_completion
except ImportError:
    raise ImportError(
        "Could not import autogen python package. "
        "Please install it with `pip install autogen-agentchat~=0.2`."
    )

logger = logging.getLogger(__name__)


@add_framework_adapter(FrameworkType.AutoGen.value)
def prepare_autogen_0_2():
    """
    Replace OpenAIWrapper and ConversableAgent methods with aios's implementation.

    This function is used to adapt autogen's API to aios's API, and it is used
    internally by aios.
    """
    # Replace OpenAIWrapper method
    OpenAIWrapper.__init__ = adapter_autogen_client_init
    OpenAIWrapper.create = adapter_client_create
    OpenAIWrapper.extract_text_or_completion_object = adapter_client_extract_text_or_completion_object

    # Replace agent method
    ConversableAgent._print_received_message = _adapter_print_received_message
    ConversableAgent._generate_oai_reply_from_client = _adapter_generate_oai_reply_from_client
    ConversableAgent.generate_tool_calls_reply = adapter_generate_tool_calls_reply
    ConversableAgent.execute_function = adapter_execute_function
    ConversableAgent._a_execute_tool_call = _adapter_a_execute_tool_call
    ConversableAgent.update_tool_signature = adapter_update_tool_signature
    ConversableAgent.__init__ = adapter_autogen_agent_init

    print("AutoGen prepare success")


def adapter_autogen_agent_init(
        self,
        name: str,
        system_message: Optional[Union[str, List]] = "You are a helpful AI Assistant.",
        is_termination_msg: Optional[Callable[[Dict], bool]] = None,
        max_consecutive_auto_reply: Optional[int] = None,
        human_input_mode: Literal["ALWAYS", "NEVER", "TERMINATE"] = "TERMINATE",
        function_map: Optional[Dict[str, Callable]] = None,
        code_execution_config: Union[Dict, Literal[False]] = False,
        llm_config: Optional[Union[Dict, Literal[False]]] = None,
        default_auto_reply: Union[str, Dict] = "",
        description: Optional[str] = None,
        chat_messages: Optional[Dict[Agent, List[Dict]]] = None,
        silent: Optional[bool] = None,
):
    self.agent_name = name

    # just save tool/function message in aios
    self.llm_config = {} if llm_config is not False else False
    self.client = None if (self.llm_config is False) else OpenAIWrapper(
        **self.llm_config,
        agent_name=self.agent_name
    )

    # we change code_execution_config below and we have to make sure we don't change the input
    # in case of UserProxyAgent, without this we could even change the default value {}
    code_execution_config = (
        code_execution_config.copy() if hasattr(code_execution_config, "copy") else code_execution_config
    )

    self._name = name
    # a dictionary of conversations, default value is list
    if chat_messages is None:
        self._oai_messages = defaultdict(list)
    else:
        self._oai_messages = chat_messages

    self._oai_system_message = [{"content": system_message, "role": "system"}]
    self._description = description if description is not None else system_message
    self._is_termination_msg = (
        is_termination_msg
        if is_termination_msg is not None
        else (lambda x: content_str(x.get("content")) == "TERMINATE")
    )
    self.silent = silent

    if logging_enabled():
        log_new_agent(self, locals())

    # Initialize standalone client cache object.
    self.client_cache = None

    self.human_input_mode = human_input_mode
    self._max_consecutive_auto_reply = (
        max_consecutive_auto_reply if max_consecutive_auto_reply is not None else self.MAX_CONSECUTIVE_AUTO_REPLY
    )
    self._consecutive_auto_reply_counter = defaultdict(int)
    self._max_consecutive_auto_reply_dict = defaultdict(self.max_consecutive_auto_reply)
    self._function_map = (
        {}
        if function_map is None
        else {name: callable for name, callable in function_map.items() if self._assert_valid_name(name)}
    )
    self._default_auto_reply = default_auto_reply
    self._reply_func_list = []
    self._human_input = []
    self.reply_at_receive = defaultdict(bool)
    self.register_reply([Agent, None], ConversableAgent.generate_oai_reply)
    self.register_reply([Agent, None], ConversableAgent.a_generate_oai_reply, ignore_async_in_sync_chat=True)

    # Setting up code execution.
    # Do not register code execution reply if code execution is disabled.
    if code_execution_config is not False:
        # If code_execution_config is None, set it to an empty dict.
        if code_execution_config is None:
            warnings.warn(
                "Using None to signal a default code_execution_config is deprecated. "
                "Use {} to use default or False to disable code execution.",
                stacklevel=2,
            )
            code_execution_config = {}
        if not isinstance(code_execution_config, dict):
            raise ValueError("code_execution_config must be a dict or False.")

        # We have got a valid code_execution_config.
        self._code_execution_config = code_execution_config

        if self._code_execution_config.get("executor") is not None:
            if "use_docker" in self._code_execution_config:
                raise ValueError(
                    "'use_docker' in code_execution_config is not valid when 'executor' is set. Use the appropriate arg in the chosen executor instead."
                )

            if "work_dir" in self._code_execution_config:
                raise ValueError(
                    "'work_dir' in code_execution_config is not valid when 'executor' is set. Use the appropriate arg in the chosen executor instead."
                )

            if "timeout" in self._code_execution_config:
                raise ValueError(
                    "'timeout' in code_execution_config is not valid when 'executor' is set. Use the appropriate arg in the chosen executor instead."
                )

            # Use the new code executor.
            self._code_executor = CodeExecutorFactory.create(self._code_execution_config)
            self.register_reply([Agent, None], ConversableAgent._generate_code_execution_reply_using_executor)
        else:
            # Legacy code execution using code_utils.
            use_docker = self._code_execution_config.get("use_docker", None)
            use_docker = decide_use_docker(use_docker)
            check_can_use_docker_or_throw(use_docker)
            self._code_execution_config["use_docker"] = use_docker
            self.register_reply([Agent, None], ConversableAgent.generate_code_execution_reply)
    else:
        # Code execution is disabled.
        self._code_execution_config = False

    self.register_reply([Agent, None], ConversableAgent.generate_tool_calls_reply)
    self.register_reply([Agent, None], ConversableAgent.a_generate_tool_calls_reply, ignore_async_in_sync_chat=True)
    self.register_reply([Agent, None], ConversableAgent.generate_function_call_reply)
    self.register_reply(
        [Agent, None], ConversableAgent.a_generate_function_call_reply, ignore_async_in_sync_chat=True
    )
    self.register_reply([Agent, None], ConversableAgent.check_termination_and_human_reply)
    self.register_reply(
        [Agent, None], ConversableAgent.a_check_termination_and_human_reply, ignore_async_in_sync_chat=True
    )

    # Registered hooks are kept in lists, indexed by hookable method, to be called in their order of registration.
    # New hookable methods should be added to this list as required to support new agent capabilities.
    self.hook_lists: Dict[str, List[Callable]] = {
        "process_last_received_message": [],
        "process_all_messages_before_reply": [],
        "process_message_before_send": [],
    }


def _adapter_print_received_message(self, message: Union[Dict, str], sender: Agent):
    iostream = IOStream.get_default()
    # print the message received
    iostream.print(colored(sender.name, "yellow"), "(to", f"{self.name}):\n", flush=True)
    message = self._message_to_dict(message)

    if message.get("tool_responses"):  # Handle tool multi-call responses
        for tool_response in message["tool_responses"]:
            self._print_received_message(tool_response, sender)
        if message.get("role") == "tool":
            return  # If role is tool, then content is just a concatenation of all tool_responses

    if message.get("role") in ["function", "tool"]:
        if message["role"] == "function":
            id_key = "name"
        else:
            id_key = "tool_call_id"
        id = message.get(id_key, "No id found")
        func_print = f"***** Response from calling {message['role']} ({id}) *****"
        iostream.print(colored(func_print, "green"), flush=True)
        iostream.print(message["content"], flush=True)
        iostream.print(colored("*" * len(func_print), "green"), flush=True)
    else:
        content = message.get("content")
        if content is not None:
            if "context" in message:
                content = OpenAIWrapper.instantiate(
                    content,
                    message["context"],
                    self.llm_config and self.llm_config.get("allow_format_str_template", False),
                )
            iostream.print(content_str(content), flush=True)
        if "function_call" in message and message["function_call"]:
            function_call = dict(message["function_call"])
            func_print = (
                f"***** Suggested function call: {function_call.get('name', '(No function name found)')} *****"
            )
            iostream.print(colored(func_print, "green"), flush=True)
            iostream.print(
                "Arguments: \n",
                function_call.get("arguments", "(No arguments found)"),
                flush=True,
                sep="",
            )
            iostream.print(colored("*" * len(func_print), "green"), flush=True)
        if "tool_calls" in message and message["tool_calls"]:
            for tool_call in message["tool_calls"]:
                id = tool_call.get("id", "No tool call id found")
                # function_call = dict(tool_call.get("function", {}))
                function_call = tool_call
                func_print = f"***** Suggested tool call ({id}): {function_call.get('name', '(No function name found)')} *****"
                iostream.print(colored(func_print, "green"), flush=True)
                iostream.print(
                    "Parameters: \n",
                    function_call.get("parameters", "(No parameters found)"),
                    flush=True,
                    sep="",
                )
                iostream.print(colored("*" * len(func_print), "green"), flush=True)

    iostream.print("\n", "-" * 80, flush=True, sep="")


def _adapter_generate_oai_reply_from_client(self, llm_client, messages, cache) -> Union[str, Dict, None]:
    # unroll tool_responses
    all_messages = []
    for message in messages:
        tool_responses = message.get("tool_responses", [])
        if tool_responses:
            all_messages += tool_responses
            # tool role on the parent message means the content is just concatenation of all of the tool_responses
            if message.get("role") != "tool":
                all_messages.append({key: message[key] for key in message if key != "tool_responses"})
        else:
            all_messages.append(message)

    # TODO: #1143 handle token limit exceeded error
    response = llm_client.create(
        context=messages[-1].pop("context", None), messages=all_messages, cache=cache, agent=self
    )
    extracted_response = llm_client.extract_text_or_completion_object(response)[0]
    if extracted_response is None:
        warnings.warn(f"Extracted_response from {response} is None.", UserWarning)
        return None
    # ensure function and tool calls will be accepted when sent back to the LLM
    if not isinstance(extracted_response, str) and hasattr(extracted_response, "model_dump"):
        extracted_response = model_dump(extracted_response)
    if isinstance(extracted_response, dict):
        if extracted_response.get("function_call"):
            extracted_response["function_call"]["name"] = self._normalize_name(
                extracted_response["function_call"]["name"]
            )
        for tool_call in extracted_response.get("tool_calls") or []:
            tool_call["name"] = self._normalize_name(tool_call["name"])
            tool_call["function"] = {"name": tool_call["name"], "arguments": str(tool_call["parameters"])}
    return extracted_response


def adapter_generate_tool_calls_reply(
        self,
        messages: Optional[List[Dict]] = None,
        sender: Optional[Agent] = None,
        config: Optional[Any] = None,
) -> Tuple[bool, Union[Dict, None]]:
    """Generate a reply using tool call."""
    if config is None:
        config = self
    if messages is None:
        messages = self._oai_messages[sender]
    message = messages[-1]
    tool_returns = []
    for tool_call in message.get("tool_calls", []):
        function_call = tool_call
        func = self._function_map.get(function_call.get("name", None), None)
        if inspect.iscoroutinefunction(func):
            try:
                # get the running loop if it was already created
                loop = asyncio.get_running_loop()
                close_loop = False
            except RuntimeError:
                # create a loop if there is no running loop
                loop = asyncio.new_event_loop()
                close_loop = True

            _, func_return = loop.run_until_complete(self.a_execute_function(function_call))
            if close_loop:
                loop.close()
        else:
            _, func_return = self.execute_function(function_call)
        content = func_return.get("content", "")
        if content is None:
            content = ""
        tool_call_id = tool_call.get("id", None)
        if tool_call_id is not None:
            tool_call_response = {
                "tool_call_id": tool_call_id,
                "role": "tool",
                "content": content,
            }
        else:
            # Do not include tool_call_id if it is not present.
            # This is to make the tool call object compatible with Mistral API.
            tool_call_response = {
                "role": "tool",
                "content": content,
            }
        tool_returns.append(tool_call_response)
    if tool_returns:
        return True, {
            "role": "tool",
            "tool_responses": tool_returns,
            "content": "\n\n".join([self._str_for_tool_response(tool_return) for tool_return in tool_returns]),
        }
    return False, None


def adapter_execute_function(self, func_call, verbose: bool = False) -> Tuple[bool, Dict[str, str]]:
    """Execute a function call and return the result.

    Override this function to modify the way to execute function and tool calls.

    Args:
        func_call: a dictionary extracted from openai message at "function_call" or "tool_calls" with keys "name" and "arguments".

    Returns:
        A tuple of (is_exec_success, result_dict).
        is_exec_success (boolean): whether the execution is successful.
        result_dict: a dictionary with keys "name", "role", and "content". Value of "role" is "function".

    "function_call" deprecated as of [OpenAI API v1.1.0](https://github.com/openai/openai-python/releases/tag/v1.1.0)
    See https://platform.openai.com/docs/api-reference/chat/create#chat-create-function_call
    """
    iostream = IOStream.get_default()

    func_name = func_call.get("name", "")
    func = self._function_map.get(func_name, None)

    is_exec_success = False
    if func is not None:
        arguments = func_call.get("parameters", None)

        # Try to execute the function
        if arguments is not None:
            iostream.print(
                colored(f"\n>>>>>>>> EXECUTING FUNCTION {func_name}...", "magenta"),
                flush=True,
            )
            try:
                content = func(**arguments)
                is_exec_success = True
            except Exception as e:
                content = f"Error: {e}"
    else:
        content = f"Error: Function {func_name} not found."

    if verbose:
        iostream.print(
            colored(f"\nInput arguments: {arguments}\nOutput:\n{content}", "magenta"),
            flush=True,
        )

    return is_exec_success, {
        "name": func_name,
        "role": "function",
        "content": str(content),
    }


async def _adapter_a_execute_tool_call(self, tool_call):
    id = tool_call["id"]
    function_call = tool_call
    _, func_return = await self.a_execute_function(function_call)
    return {
        "tool_call_id": id,
        "role": "tool",
        "content": func_return.get("content", ""),
    }


def adapter_update_tool_signature(self, tool_sig: Union[str, Dict], is_remove: None):
    """update a tool_signature in the LLM configuration for tool_call.

    Args:
        tool_sig (str or dict): description/name of the tool to update/remove to the model. See: https://platform.openai.com/docs/api-reference/chat/create#chat-create-tools
        is_remove: whether removing the tool from llm_config with name 'tool_sig'
    """

    if is_remove:
        if "tools" not in self.llm_config.keys():
            error_msg = "The agent config doesn't have tool {name}.".format(name=tool_sig)
            logger.error(error_msg)
            raise AssertionError(error_msg)
        else:
            self.llm_config["tools"] = [
                tool for tool in self.llm_config["tools"] if tool["function"]["name"] != tool_sig
            ]
    else:
        if not isinstance(tool_sig, dict):
            raise ValueError(
                f"The tool signature must be of the type dict. Received tool signature type {type(tool_sig)}"
            )
        self._assert_valid_name(tool_sig["function"]["name"])
        if "tools" in self.llm_config:
            if any(tool["function"]["name"] == tool_sig["function"]["name"] for tool in self.llm_config["tools"]):
                warnings.warn(f"Function '{tool_sig['function']['name']}' is being overridden.", UserWarning)
            self.llm_config["tools"] = [
                                           tool
                                           for tool in self.llm_config["tools"]
                                           if tool.get("function", {}).get("name") != tool_sig["function"]["name"]
                                       ] + [tool_sig]
        else:
            self.llm_config["tools"] = [tool_sig]

    if len(self.llm_config["tools"]) == 0:
        del self.llm_config["tools"]

    self.client = OpenAIWrapper(
        **self.llm_config,
        agent_name=self.agent_name
    )


def adapter_autogen_client_init(self, *,
                                config_list: Optional[List[Dict[str, Any]]] = None,
                                agent_name: Optional[str],
                                **base_config: Any):
    if agent_name:
        self.agent_name = agent_name

    if logging_enabled():
        log_new_wrapper(self, locals())
    _, extra_kwargs = self._separate_openai_config(base_config)
    # It's OK if "model" is not provided in base_config or config_list
    # Because one can provide "model" at `create` time.

    self._clients: List[ModelClient] = []
    self._config_list: List[Dict[str, Any]] = []

    self._config_list = [extra_kwargs]
    self.wrapper_id = id(self)


def adapter_client_create(self, **config: Any) -> ModelClient.ModelClientResponseProtocol:
    # if ERROR:
    #     raise ERROR
    invocation_id = str(uuid.uuid4())
    last = len(self._clients) - 1
    # Check if all configs in config list are activated
    non_activated = [
        client.config["model_client_cls"] for client in self._clients if isinstance(client, PlaceHolderClient)
    ]
    if non_activated:
        raise RuntimeError(
            f"Model client(s) {non_activated} are not activated. Please register the custom model clients using "
            f"`register_model_client` or filter them out form the config list."
        )
    for i, client in enumerate([None]):
        # merge the input config with the i-th config in the config list
        full_config = {**config, **self._config_list[i]}
        # separate the config into create_config and extra_kwargs
        create_config, extra_kwargs = self._separate_create_config(full_config)
        api_type = extra_kwargs.get("api_type")
        if api_type and api_type.startswith("azure") and "model" in create_config:
            create_config["model"] = create_config["model"].replace(".", "")
        # construct the create params
        params = self._construct_create_params(create_config, extra_kwargs)
        # get the cache_seed, filter_func and context
        cache_seed = extra_kwargs.get("cache_seed", LEGACY_DEFAULT_CACHE_SEED)
        cache = extra_kwargs.get("cache")
        filter_func = extra_kwargs.get("filter_func")
        context = extra_kwargs.get("context")
        agent = extra_kwargs.get("agent")
        price = extra_kwargs.get("price", None)
        if isinstance(price, list):
            price = tuple(price)
        elif isinstance(price, float) or isinstance(price, int):
            logger.warning(
                "Input price is a float/int. Using the same price for prompt and completion tokens. Use a list/tuple "
                "if prompt and completion token prices are different."
            )

        cache_client = None
        if cache is not None:
            # Use the cache object if provided.
            cache_client = cache
        elif cache_seed is not None:
            # Legacy cache behavior, if cache_seed is given, use DiskCache.
            cache_client = Cache.disk(cache_seed, LEGACY_CACHE_DIR)

        if cache_client is not None:
            with cache_client as cache:
                # Try to get the response from cache
                key = get_key(params)
                request_ts = get_current_ts()

                response: ModelClient.ModelClientResponseProtocol = cache.get(key, None)

                if response is not None:
                    # response.message_retrieval_function = client.message_retrieval
                    # try:
                    #     response.cost  # type: ignore [attr-defined]
                    # except AttributeError:
                    #     # update attribute if cost is not calculated
                    #     response.cost = client.cost(response)
                    #     cache.set(key, response)
                    # total_usage = client.get_usage(response)

                    if logging_enabled():
                        # Log the cache hit
                        # TODO: log the config_id and pass_filter etc.
                        log_chat_completion(
                            invocation_id=invocation_id,
                            client_id=id(client),
                            wrapper_id=id(self),
                            agent=agent,
                            request=params,
                            response=response,
                            is_cached=1,
                            cost=response.cost,
                            start_time=request_ts,
                        )

                    # check the filter
                    pass_filter = filter_func is None or filter_func(context=context, response=response)
                    if pass_filter or i == last:
                        return response
                    continue  # filter is not passed; try the next config
        try:
            request_ts = get_current_ts()
            send_request = get_request_func()
            response = send_request(
                query=LLMQuery(
                    messages=params['messages'],
                    tools=(params["tools"] if "tools" in params else None)
                )
            )["response"]
            response = {'content': response["response_message"], 'tool_calls': response["tool_calls"]}
        except APITimeoutError as err:
            logger.debug(f"config {i} timed out", exc_info=True)
            if i == last:
                raise TimeoutError(
                    "OpenAI API call timed out. This could be due to congestion or too small a timeout value. The "
                    "timeout can be specified by setting the 'timeout' value (in seconds) in the llm_config (if you "
                    "are using agents) or the OpenAIWrapper constructor (if you are using the OpenAIWrapper directly)."
                ) from err
        except APIError as err:
            error_code = getattr(err, "code", None)
            if logging_enabled():
                log_chat_completion(
                    invocation_id=invocation_id,
                    client_id=id(client),
                    wrapper_id=id(self),
                    agent=agent,
                    request=params,
                    response=f"error_code:{error_code}, config {i} failed",
                    is_cached=0,
                    cost=0,
                    start_time=request_ts,
                )

            if error_code == "content_filter":
                # raise the error for content_filter
                raise
            logger.debug(f"config {i} failed", exc_info=True)
            if i == last:
                raise
        else:
            if cache_client is not None:
                # Cache the response
                with cache_client as cache:
                    cache.set(key, response)

            if logging_enabled():
                log_chat_completion(
                    invocation_id=invocation_id,
                    client_id=id(client),
                    wrapper_id=id(self),
                    agent=agent,
                    request=params,
                    response=response,
                    is_cached=0,
                    cost=response.cost,
                    start_time=request_ts,
                )

            # response.message_retrieval_function = client.message_retrieval
            # check the filter
            pass_filter = filter_func is None or filter_func(context=context, response=response)
            if pass_filter or i == last:
                return response
            continue  # filter is not passed; try the next config
    raise RuntimeError("Should not reach here.")


def adapter_client_extract_text_or_completion_object(
        cls, response: ModelClient.ModelClientResponseProtocol
) -> Union[List[str], List[ModelClient.ModelClientResponseProtocol.Choice.Message]]:
    """Extract the text or ChatCompletion objects from a completion or chat response.

    Args:
        response (ChatCompletion | Completion): The response from openai.

    Returns:
        A list of text, or a list of ChatCompletion objects if function_call/tool_calls are present.
    """
    return [response]
