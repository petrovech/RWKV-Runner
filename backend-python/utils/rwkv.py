import os
import pathlib
import copy
from typing import Dict, List, Tuple
from utils.log import quick_log
from fastapi import HTTPException
from pydantic import BaseModel, Field
import torch
import numpy as np
from rwkv_pip.utils import PIPELINE
from routes import state_cache


END_OF_TEXT = 0
END_OF_LINE = 187
END_OF_LINE_DOUBLE = 535


os.environ["TORCH_EXTENSIONS_DIR"] = f"{pathlib.Path(__file__).parent.parent.resolve()}"


class RWKV:
    def __init__(self, model: str, strategy: str, tokens_path: str) -> None:
        from rwkv.model import RWKV as Model  # dynamic import to make RWKV_CUDA_ON work

        filename, _ = os.path.splitext(os.path.basename(model))
        self.name = filename
        self.model = Model(model, strategy)
        self.pipeline = PIPELINE(self.model, tokens_path)
        self.model_state = None
        self.model_tokens = []

        self.CHUNK_LEN = 256

        self.max_tokens_per_generation = 500
        self.temperature = 1
        self.top_p = 0.5
        self.penalty_alpha_presence = 0.4
        self.penalty_alpha_frequency = 0.4

        self.interface = ":"
        if "rwkv_vocab" in tokens_path:
            self.user = "Question"
            self.bot = "Answer"
        else:
            self.user = "Bob"
            self.bot = "Alice"

        self.AVOID_REPEAT_TOKENS = []
        AVOID_REPEAT = "，：？！"
        for i in AVOID_REPEAT:
            dd = self.pipeline.encode(i)
            assert len(dd) == 1
            self.AVOID_REPEAT_TOKENS += dd

        self.preload()

    def preload(self):
        interface = self.interface
        user = self.user
        bot = self.bot
        preset_system = (
            f"""
The following is a coherent verbose detailed conversation between a girl named {bot} and her friend {user}. \
{bot} is very intelligent, creative and friendly. \
{bot} is unlikely to disagree with {user}, and {bot} doesn't like to ask {user} questions. \
{bot} likes to tell {user} a lot about herself and her opinions. \
{bot} usually gives {user} kind, helpful and informative advices.\n
"""
            if self.user == "Bob"
            else f"{user}{interface} hi\n\n{bot}{interface} Hi. "
            + "I am your assistant and I will provide expert full response in full details. Please feel free to ask any question and I will always answer it.\n\n"
        )
        logits, _ = self.run_rnn(self.fix_tokens(self.pipeline.encode(preset_system)))
        try:
            state_cache.add_state(
                state_cache.AddStateBody(
                    prompt=preset_system,
                    tokens=self.model_tokens,
                    state=self.model_state,
                    logits=logits,
                )
            )
        except HTTPException:
            pass

    # Model only saw '\n\n' as [187, 187] before, but the tokenizer outputs [535] for it at the end
    def fix_tokens(self, tokens):
        if len(tokens) > 0 and tokens[-1] == END_OF_LINE_DOUBLE:
            tokens = tokens[:-1] + [END_OF_LINE, END_OF_LINE]
        return tokens

    def run_rnn(self, _tokens: List[str], newline_adj: int = 0):
        tokens = [int(x) for x in _tokens]
        token_len = len(tokens)
        self.model_tokens += tokens

        while len(tokens) > 0:
            out, self.model_state = self.model.forward(
                tokens[: self.CHUNK_LEN], self.model_state
            )
            tokens = tokens[self.CHUNK_LEN :]

        out[END_OF_LINE] += newline_adj  # adjust \n probability

        if self.model_tokens[-1] in self.AVOID_REPEAT_TOKENS:
            out[self.model_tokens[-1]] = -999999999
        return out, token_len

    def get_embedding(self, input: str, fast_mode: bool) -> Tuple[List[float], int]:
        if fast_mode:
            embedding, token_len = self.fast_embedding(
                self.fix_tokens(self.pipeline.encode(input)), None
            )
        else:
            self.model_state = None
            self.model_tokens = []
            _, token_len = self.run_rnn(self.fix_tokens(self.pipeline.encode(input)))
            embedding = self.model_state[-5].tolist()
        embedding = (embedding / np.linalg.norm(embedding)).tolist()
        return embedding, token_len

    def fast_embedding(self, tokens: List[str], state):
        tokens = [int(x) for x in tokens]
        token_len = len(tokens)
        self = self.model

        with torch.no_grad():
            w = self.w
            args = self.args

            if state == None:
                state = [None] * args.n_layer * 5
                for i in range(
                    args.n_layer
                ):  # state: 0=att_xx 1=att_aa 2=att_bb 3=att_pp 4=ffn_xx
                    dd = self.strategy[i]
                    dev = dd.device
                    atype = dd.atype
                    state[i * 5 + 0] = torch.zeros(
                        args.n_embd, dtype=atype, requires_grad=False, device=dev
                    ).contiguous()
                    state[i * 5 + 1] = torch.zeros(
                        args.n_embd, dtype=torch.float, requires_grad=False, device=dev
                    ).contiguous()
                    state[i * 5 + 2] = torch.zeros(
                        args.n_embd, dtype=torch.float, requires_grad=False, device=dev
                    ).contiguous()
                    state[i * 5 + 3] = (
                        torch.zeros(
                            args.n_embd,
                            dtype=torch.float,
                            requires_grad=False,
                            device=dev,
                        ).contiguous()
                        - 1e30
                    )
                    state[i * 5 + 4] = torch.zeros(
                        args.n_embd, dtype=atype, requires_grad=False, device=dev
                    ).contiguous()

                    break

            seq_mode = len(tokens) > 1

            x = w["emb.weight"][tokens if seq_mode else tokens[0]]

            for i in range(args.n_layer):
                bbb = f"blocks.{i}."
                att = f"blocks.{i}.att."
                ffn = f"blocks.{i}.ffn."
                dd = self.strategy[i]
                dev = dd.device
                atype = dd.atype
                wtype = dd.wtype
                if seq_mode:
                    if "cuda" in str(dev) and os.environ["RWKV_CUDA_ON"] == "1":
                        ATT = (
                            self.cuda_att_seq
                            if wtype != torch.uint8
                            else self.cuda_att_seq_i8
                        )
                    else:
                        ATT = self.att_seq if wtype != torch.uint8 else self.att_seq_i8
                    FFN = self.ffn_seq if wtype != torch.uint8 else self.ffn_seq_i8
                else:
                    ATT = self.att_one if wtype != torch.uint8 else self.att_one_i8
                    FFN = self.ffn_one if wtype != torch.uint8 else self.ffn_one_i8

                x = x.to(dtype=atype, device=dev)

                kw = w[f"{att}key.weight"]
                vw = w[f"{att}value.weight"]
                rw = w[f"{att}receptance.weight"]
                ow = w[f"{att}output.weight"]
                if dd.stream:
                    kw = kw.to(device=dev, non_blocking=True)
                    vw = vw.to(device=dev, non_blocking=True)
                    rw = rw.to(device=dev, non_blocking=True)
                    ow = ow.to(device=dev, non_blocking=True)
                kmx = w[f"{att}key.weight_mx"] if wtype == torch.uint8 else x
                krx = w[f"{att}key.weight_rx"] if wtype == torch.uint8 else x
                kmy = w[f"{att}key.weight_my"] if wtype == torch.uint8 else x
                kry = w[f"{att}key.weight_ry"] if wtype == torch.uint8 else x
                vmx = w[f"{att}value.weight_mx"] if wtype == torch.uint8 else x
                vrx = w[f"{att}value.weight_rx"] if wtype == torch.uint8 else x
                vmy = w[f"{att}value.weight_my"] if wtype == torch.uint8 else x
                vry = w[f"{att}value.weight_ry"] if wtype == torch.uint8 else x
                rmx = w[f"{att}receptance.weight_mx"] if wtype == torch.uint8 else x
                rrx = w[f"{att}receptance.weight_rx"] if wtype == torch.uint8 else x
                rmy = w[f"{att}receptance.weight_my"] if wtype == torch.uint8 else x
                rry = w[f"{att}receptance.weight_ry"] if wtype == torch.uint8 else x
                omx = w[f"{att}output.weight_mx"] if wtype == torch.uint8 else x
                orx = w[f"{att}output.weight_rx"] if wtype == torch.uint8 else x
                omy = w[f"{att}output.weight_my"] if wtype == torch.uint8 else x
                ory = w[f"{att}output.weight_ry"] if wtype == torch.uint8 else x
                (
                    x,
                    state[i * 5 + 0],
                    state[i * 5 + 1],
                    state[i * 5 + 2],
                    state[i * 5 + 3],
                ) = ATT(
                    x,
                    state[i * 5 + 0],
                    state[i * 5 + 1],
                    state[i * 5 + 2],
                    state[i * 5 + 3],
                    w[f"{bbb}ln1.weight"],
                    w[f"{bbb}ln1.bias"],
                    w[f"{att}time_mix_k"],
                    w[f"{att}time_mix_v"],
                    w[f"{att}time_mix_r"],
                    w[f"{att}time_decay"],
                    w[f"{att}time_first"],
                    kw,
                    vw,
                    rw,
                    ow,
                    kmx,
                    krx,
                    kmy,
                    kry,
                    vmx,
                    vrx,
                    vmy,
                    vry,
                    rmx,
                    rrx,
                    rmy,
                    rry,
                    omx,
                    orx,
                    omy,
                    ory,
                )

                return state[0].tolist(), token_len

    def generate(self, prompt: str, stop: str = None):
        quick_log(None, None, "Generation Prompt:\n" + prompt)
        cache = None
        delta_prompt = prompt
        try:
            cache = state_cache.longest_prefix_state(
                state_cache.LongestPrefixStateBody(prompt=prompt), None
            )
        except HTTPException:
            pass
        if cache is None or cache["prompt"] == "":
            self.model_state = None
            self.model_tokens = []
        else:
            delta_prompt = prompt[len(cache["prompt"]) :]
            self.model_state = copy.deepcopy(cache["state"])
            self.model_tokens = copy.deepcopy(cache["tokens"])
            logits = copy.deepcopy(cache["logits"])

        prompt_token_len = 0
        if delta_prompt != "":
            logits, prompt_token_len = self.run_rnn(
                self.fix_tokens(self.pipeline.encode(delta_prompt))
            )
            try:
                state_cache.add_state(
                    state_cache.AddStateBody(
                        prompt=prompt,
                        tokens=self.model_tokens,
                        state=self.model_state,
                        logits=logits,
                    )
                )
            except HTTPException:
                pass

        begin = len(self.model_tokens)
        out_last = begin

        occurrence: Dict = {}

        completion_token_len = 0
        response = ""
        for i in range(self.max_tokens_per_generation):
            for n in occurrence:
                logits[n] -= (
                    self.penalty_alpha_presence
                    + occurrence[n] * self.penalty_alpha_frequency
                )
            token = self.pipeline.sample_logits(
                logits, temperature=self.temperature, top_p=self.top_p
            )

            if token == END_OF_TEXT:
                yield response, "", prompt_token_len, completion_token_len
                break
            if token not in occurrence:
                occurrence[token] = 1
            else:
                occurrence[token] += 1

            logits, _ = self.run_rnn([token])
            completion_token_len = completion_token_len + 1
            delta: str = self.pipeline.decode(self.model_tokens[out_last:])
            if "\ufffd" not in delta:  # avoid utf-8 display issues
                response += delta
                if stop is not None:
                    if stop in response:
                        response = response.split(stop)[0]
                        try:
                            state_cache.add_state(
                                state_cache.AddStateBody(
                                    prompt=prompt + response,
                                    tokens=self.model_tokens,
                                    state=self.model_state,
                                    logits=logits,
                                )
                            )
                        except HTTPException:
                            pass
                        yield response, "", prompt_token_len, completion_token_len
                        break
                out_last = begin + i + 1
                if i == self.max_tokens_per_generation - 1:
                    try:
                        state_cache.add_state(
                            state_cache.AddStateBody(
                                prompt=prompt + response,
                                tokens=self.model_tokens,
                                state=self.model_state,
                                logits=logits,
                            )
                        )
                    except HTTPException:
                        pass
                yield response, delta, prompt_token_len, completion_token_len


class ModelConfigBody(BaseModel):
    max_tokens: int = Field(default=None, gt=0, le=102400)
    temperature: float = Field(default=None, ge=0, le=2)
    top_p: float = Field(default=None, ge=0, le=1)
    presence_penalty: float = Field(default=None, ge=-2, le=2)
    frequency_penalty: float = Field(default=None, ge=-2, le=2)

    class Config:
        schema_extra = {
            "example": {
                "max_tokens": 1000,
                "temperature": 1.2,
                "top_p": 0.5,
                "presence_penalty": 0.4,
                "frequency_penalty": 0.4,
            }
        }


def set_rwkv_config(model: RWKV, body: ModelConfigBody):
    if body.max_tokens is not None:
        model.max_tokens_per_generation = body.max_tokens
    if body.temperature is not None:
        if body.temperature < 0.1:
            model.temperature = 0.1
        else:
            model.temperature = body.temperature
    if body.top_p is not None:
        model.top_p = body.top_p
    if body.presence_penalty is not None:
        model.penalty_alpha_presence = body.presence_penalty
    if body.frequency_penalty is not None:
        model.penalty_alpha_frequency = body.frequency_penalty


def get_rwkv_config(model: RWKV) -> ModelConfigBody:
    return ModelConfigBody(
        max_tokens=model.max_tokens_per_generation,
        temperature=model.temperature,
        top_p=model.top_p,
        presence_penalty=model.penalty_alpha_presence,
        frequency_penalty=model.penalty_alpha_frequency,
    )
