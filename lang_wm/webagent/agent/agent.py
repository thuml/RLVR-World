# This file is from WMA project:
# https://github.com/kyle8581/WMA-Agents

import argparse
import random
import re
import json
import time
from typing import Any, Optional
import os

import tiktoken
from beartype import beartype
from PIL import Image

from agent.prompts import *
from browser_env import Trajectory
from browser_env.actions import (
    Action,
    ActionParsingError,
    create_id_based_action,
    create_none_action,
    create_playwright_action,
)
from browser_env.utils import Observation, StateInfo
from langchain_openai import ChatOpenAI
from langchain_core.prompts import PromptTemplate, ChatPromptTemplate
from llms import (
    call_llm,
    generate_from_huggingface_completion,
    generate_from_openai_chat_completion,
    generate_from_openai_completion,
    lm_config,
)
from llms.tokenizers import Tokenizer

from evaluation_harness import CaptioningFn
import numpy.typing as npt
import numpy as np
from filelock import FileLock

class Agent:
    """Base class for the agent"""

    def __init__(self, *args: Any) -> None:
        pass

    def next_action(
        self,
        trajectory: Trajectory,
        intent: str,
        meta_data: dict[str, Any],
        *args,
        **kwargs
    ) -> Action | list[Action]:
        """Predict the next action given the observation"""
        raise NotImplementedError

    def reset(
        self,
        test_config_file: str,
    ) -> None:
        raise NotImplementedError


class TeacherForcingAgent(Agent):
    """Agent that follows a pre-defined action sequence"""

    def __init__(self) -> None:
        super().__init__()

    def set_action_set_tag(self, tag: str) -> None:
        self.action_set_tag = tag

    def set_actions(self, action_seq: str | list[str]) -> None:
        if isinstance(action_seq, str):
            action_strs = action_seq.strip().split("\n")
        else:
            action_strs = action_seq
        action_strs = [a.strip() for a in action_strs]

        actions = []
        for a_str in action_strs:
            try:
                if self.action_set_tag == "playwright":
                    cur_action = create_playwright_action(a_str)
                elif self.action_set_tag == "id_accessibility_tree":
                    cur_action = create_id_based_action(a_str)
                else:
                    raise ValueError(
                        f"Unknown action type {self.action_set_tag}"
                    )
            except ActionParsingError as e:
                cur_action = create_none_action()

            cur_action["raw_prediction"] = a_str
            actions.append(cur_action)

        self.actions: list[Action] = actions

    def next_action(
        self,
        trajectory: Trajectory,
        intent: str,
        meta_data: dict[str, Any]
    ) -> Action:
        """Predict the next action given the observation"""
        return self.actions.pop(0)

    def reset(
        self,
        test_config_file: str,
    ) -> None:
        with open(test_config_file) as f:
            ref_actions = json.load(f)["reference_action_sequence"]
            tag = ref_actions["action_set_tag"]
            action_seq = ref_actions["action_sequence"]
            self.set_action_set_tag(tag)
            self.set_actions(action_seq)

class PromptAgent(Agent):
    """prompt-based agent that emits action given the history"""

    @beartype
    def __init__(
        self,
        action_set_tag: str,
        lm_config: lm_config.LMConfig,
        prompt_constructor: PromptConstructor,
        captioning_fn: Optional[CaptioningFn] = None
    ) -> None:
        super().__init__()
        self.lm_config = lm_config
        self.prompt_constructor = prompt_constructor
        self.action_set_tag = action_set_tag
        self.captioning_fn = captioning_fn

        # Check if the model is multimodal.
        if ("gemini" in lm_config.model or "gpt-4" in lm_config.model and "vision" in lm_config.model or "gpt-4o" in lm_config.model) and type(prompt_constructor) == MultimodalCoTPromptConstructor:
            self.multimodal_inputs = True
        else:
            self.multimodal_inputs = False

    def set_action_set_tag(self, tag: str) -> None:
        self.action_set_tag = tag

    @beartype
    def next_action(
        self,
        trajectory: Trajectory,
        intent: str,
        meta_data: dict[str, Any],
        images: Optional[list[Image.Image]] = None,
        output_response: bool = False,
        branching_factor: int = 1
    ) -> Action:
        del branching_factor  # Not used in prompt agent.
        # Create page screenshot image for multimodal models.
        if self.multimodal_inputs:
            page_screenshot_arr: npt.NDArray[np.uint8]\
                = trajectory[-1]["observation"]["image"] # type: ignore
            page_screenshot_img = Image.fromarray(
                page_screenshot_arr
            )  # size = (viewport_width, viewport_width)

        # Caption the input image, if provided.
        if images is not None and len(images) > 0:
            if self.captioning_fn is not None:
                image_input_caption = ""
                for image_i, image in enumerate(images):
                    if image_i == 0:
                        image_input_caption += f'Input image {image_i+1}: "{self.captioning_fn([image])[0]}"'
                    else:
                        image_input_caption += f'input image {image_i+1}: "{self.captioning_fn([image])[0]}"'
                    if len(images) > 1:
                        image_input_caption += ", "
                # Update intent to include captions of input images.
                intent = f"{image_input_caption}\nIntent: {intent}"
            elif not self.multimodal_inputs:
                print(
                    "WARNING: Input image provided but no image captioner available."
                )

        if self.multimodal_inputs:
            prompt = self.prompt_constructor.construct(
                trajectory, intent, meta_data,
                page_screenshot_img=page_screenshot_img,
                images=images
            )
        else:
            prompt = self.prompt_constructor.construct(
                trajectory, intent, meta_data
            )
        lm_config = self.lm_config
        n = 0
        while True:
            response = call_llm(lm_config, prompt)
            force_prefix = self.prompt_constructor.instruction[
                "meta_data"
            ].get("force_prefix", "")
            response = f"{force_prefix}{response}"
            if output_response:
                print(f'Agent: {response}', flush=True)
            n += 1
            try:
                parsed_response = self.prompt_constructor.extract_action(
                    response
                )
                if self.action_set_tag == "id_accessibility_tree":
                    action = create_id_based_action(parsed_response)
                elif self.action_set_tag == "playwright":
                    action = create_playwright_action(parsed_response)
                elif self.action_set_tag == "som":
                    action = create_id_based_action(parsed_response)
                else:
                    raise ValueError(
                        f"Unknown action type {self.action_set_tag}"
                    )
                action["raw_prediction"] = response
                break
            except ActionParsingError as e:
                if n >= lm_config.gen_config["max_retry"]:
                    action = create_none_action()
                    action["raw_prediction"] = response
                    break

        return action

    def reset(self, test_config_file: str) -> None:
        pass


class SearchAgent(Agent):
    """prompt-based agent with search that emits action given the history"""

    def __init__(
        self,
        action_set_tag: str,
        lm_config: lm_config.LMConfig,
        prompt_constructor: PromptConstructor,
        captioning_fn: Optional[CaptioningFn] = None,
    ) -> None:
        super().__init__()
        self.lm_config = lm_config
        self.prompt_constructor = prompt_constructor
        self.action_set_tag = action_set_tag
        self.captioning_fn = captioning_fn

        # Check if the model is multimodal.
        if (
            "gemini" in lm_config.model
            or ("gpt-4" in lm_config.model and "vision" in lm_config.model)
            or "gpt-4o" in lm_config.model
        ) and isinstance(prompt_constructor, MultimodalCoTPromptConstructor):
            self.multimodal_inputs = True
        else:
            self.multimodal_inputs = False


    def set_action_set_tag(self, tag: str) -> None:
        self.action_set_tag = tag

    def next_action(
        self,
        trajectory: Trajectory,
        intent: str,
        meta_data: dict[str, Any],
        images: Optional[list[Image.Image]] = None,
        output_response: bool = False,
        branching_factor: int = 5
    ) -> list[Action]:
        if output_response:
            print("Using SearchAgent, branching_factor =", branching_factor)
        # Create page screenshot image for multimodal models.
        if self.multimodal_inputs:
            page_screenshot_arr: npt.NDArray[np.uint8]\
                = trajectory[-1]["observation"]["image"] # type: ignore
            page_screenshot_img = Image.fromarray(
                page_screenshot_arr
            )  # size = (viewport_width, viewport_width)

        # Caption the input image, if provided.
        if images is not None and len(images) > 0:
            if self.captioning_fn is not None:
                image_input_caption = ""
                for image_i, image in enumerate(images):
                    if image_i == 0:
                        image_input_caption += f'Input image {image_i+1}: "{self.captioning_fn([image])[0]}"'
                    else:
                        image_input_caption += f'input image {image_i+1}: "{self.captioning_fn([image])[0]}"'
                    if len(images) > 1:
                        image_input_caption += ", "
                # Update intent to include captions of input images.
                intent = f"{image_input_caption}\nIntent: {intent}"
            elif not self.multimodal_inputs:
                print(
                    "WARNING: Input image provided but no image captioner available."
                )

        if self.multimodal_inputs:
            prompt = self.prompt_constructor.construct(
                trajectory, intent, meta_data,
                page_screenshot_img=page_screenshot_img,
                images=images,
            )
        else:
            prompt = self.prompt_constructor.construct(
                trajectory, intent, meta_data
            )
        lm_config = self.lm_config
        n = 0
        while True:
            responses = call_llm(lm_config, prompt, num_outputs=max(branching_factor * 2, 20))
            if output_response:
                print(f'Agent: {responses}', flush=True)
            if type(responses) == str:
                responses = [responses]
            force_prefix = self.prompt_constructor.instruction[
                "meta_data"
            ].get("force_prefix", "")
            n += 1
            all_actions = {}
            parsed_actions_count: dict[str, int] = {}

            for response in responses:
                response = f"{force_prefix}{response}"
                try:
                    parsed_response = self.prompt_constructor.extract_action(
                        response
                    )
                    if parsed_response in all_actions:
                        parsed_actions_count[parsed_response] += 1
                    else:
                        if self.action_set_tag == "id_accessibility_tree":
                            action = create_id_based_action(parsed_response)
                        elif self.action_set_tag == "playwright":
                            action = create_playwright_action(parsed_response)
                        elif self.action_set_tag == "som":
                            action = create_id_based_action(parsed_response)
                        else:
                            raise ValueError(
                                f"Unknown action type {self.action_set_tag}"
                            )
                        parsed_actions_count[parsed_response] = 1
                        action["raw_prediction"] = response
                        all_actions[parsed_response] = action
                except ActionParsingError as e:
                    continue

            # If any valid action is found, break.
            if len(all_actions) > 0:
                break
            else:
                # If no valid action is found, retry.
                # If the number of retries exceeds the maximum, return a None action.
                if n >= lm_config.gen_config["max_retry"]:
                    action = create_none_action()
                    action["raw_prediction"] = response
                    return [action]

        # Find top branching_factor actions.
        top_actions = sorted(
            parsed_actions_count,
            key=parsed_actions_count.__getitem__,
            reverse=True
        )[:branching_factor]
        top_action_count = sum([parsed_actions_count[action] for action in top_actions])
        updated_actions = []
        for action_ in top_actions:
            a = all_actions[action_]
            a['prob'] = parsed_actions_count[action_] / top_action_count
            updated_actions.append(a)

        return updated_actions

    def reset(self, test_config_file: str) -> None:
        pass


def construct_agent(
    args: argparse.Namespace,
    captioning_fn: Optional[CaptioningFn] = None
) -> Agent:
    llm_config = lm_config.construct_llm_config(args)

    agent: Agent
    if args.agent_type == "teacher_forcing":
        agent = TeacherForcingAgent()
    elif args.agent_type == "prompt":
        with open(args.instruction_path) as f:
            constructor_type = json.load(f)["meta_data"]["prompt_constructor"]
        tokenizer = Tokenizer(args.provider, args.model)
        prompt_constructor = eval(constructor_type)(
            args.instruction_path, lm_config=llm_config, tokenizer=tokenizer
        )
        agent = PromptAgent(
            action_set_tag=args.action_set_tag,
            lm_config=llm_config,
            prompt_constructor=prompt_constructor,
            captioning_fn=captioning_fn
        )
    elif args.agent_type == "search":
        with open(args.instruction_path) as f:
            constructor_type = json.load(f)["meta_data"]["prompt_constructor"]
        tokenizer = Tokenizer(args.provider, args.model)
        prompt_constructor = eval(constructor_type)(
            args.instruction_path, lm_config=llm_config, tokenizer=tokenizer
        )
        agent = SearchAgent(
            action_set_tag=args.action_set_tag,
            lm_config=llm_config,
            prompt_constructor=prompt_constructor,
            captioning_fn=captioning_fn
        )
    elif args.agent_type == "world_model":
        import importlib

        WMAgent = importlib.import_module('agent.world_model_agent').WMAgent
        print(f"loading json from {args.instruction_path}...")
        max_retries = 100
        min_delay = 0.05  # 最小等待时间（秒）
        max_delay = 0.2  # 最大等待时间（秒）
        for attempt in range(max_retries):
            try:
                with open(args.instruction_path, "r") as f:
                    constructor_type = json.load(f)["meta_data"]["prompt_constructor"]
                break  # 成功读取则退出循环
            except json.JSONDecodeError as e:
                print(f"[Attempt {attempt + 1}/{max_retries}] Error decoding JSON: {e}")
                time.sleep(random.uniform(min_delay, max_delay))
            except Exception as e:
                print(f"[Attempt {attempt + 1}/{max_retries}] Other error: {e}")
                time.sleep(random.uniform(min_delay, max_delay))
        else:
            raise RuntimeError(
                f"Failed to read valid JSON from {args.instruction_path} after {max_retries} attempts.")
        # tokenizer = Tokenizer(args.provider, args.model)
        agent = WMAgent(
            agent_type = args.agent_type,
            branching_factor=args.branching_factor,
            action_set_tag=args.action_set_tag,
            vf_budget = args.vf_budget,
            model_name=args.model,
            action_prediction_prompt_path=args.instruction_path,
            state_prediction_prompt_path=args.state_prediction_prompt_path,
            value_function_prompt_path=args.value_function_prompt_path,
            world_model_training = args.world_model_training,
            world_model_name = args.world_model_name,
            world_model_url = args.world_model_url,
            value_model_training = args.value_model_training,
            value_model_name = args.value_model_name,
            value_model_url = args.value_model_url,
            top_p=args.top_p,
            temperature=args.temperature,
            my_world_model=args.my_world_model,
        )
        print("===================== World model is initialized")

    elif args.agent_type == "baseline":
        import importlib
        WMAgent = importlib.import_module('agent.world_model_agent').WMAgent
        lock = FileLock(args.instruction_path + ".lock")
        with lock:
            with open(args.instruction_path) as f:
                constructor_type = json.load(f)["meta_data"]["prompt_constructor"]
        # tokenizer = Tokenizer(args.provider, args.model)
        agent = WMAgent(
            agent_type = args.agent_type,
            action_prediction_prompt_path=args.instruction_path,
            state_prediction_prompt_path=args.state_prediction_prompt_path,
            model_name=args.model,
            value_function_prompt_path=args.value_function_prompt_path,
            branching_factor=args.branching_factor,
            action_set_tag=args.action_set_tag,
            vf_budget = args.vf_budget,
            world_model_training = args.world_model_training
        )
        print("===================== Baseline model is initialized")
    return agent
