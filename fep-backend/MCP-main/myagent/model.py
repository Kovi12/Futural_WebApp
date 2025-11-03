# MCP-main/myagent/model.py
from __future__ import annotations
from typing import Any, Optional

# Optional llama.cpp backend (unused here)
try:
    from llama_cpp import Llama  # optional
except Exception:
    Llama = None  # type: ignore

class BaseModel:
    def generate(self, prompt: str, **kwargs) -> str:
        raise NotImplementedError

    def generate_tool_call(self, prompt: str, tools: Any, **kwargs) -> str:
        return self.generate(prompt, **kwargs)

# ---------------- Unsloth backend ----------------
class UnslothModel(BaseModel):
    """
    Text-generation model using Unsloth. Loads base + optional LoRA adapter.
    """
    def __init__(
        self,
        model_id: str,
        *,
        adapter: Optional[str] = None,
        max_new_tokens: int = 384,
        load_in_4bit: bool = True,
        dtype: str = "float16",
        device_map: str = "auto",
        do_sample: bool = False,
        temperature: float = 0.0,
        top_p: float = 0.95,
        max_seq_length: int = 4096,
    ):
        # Unsloth must be imported before transformers
        from unsloth import FastLanguageModel
        from transformers import pipeline
        import torch

        self.model_id = model_id
        self.max_new_tokens = max_new_tokens
        self.gen_kwargs = dict(do_sample=do_sample, temperature=temperature, top_p=top_p)

        model, tokenizer = FastLanguageModel.from_pretrained(
            model_name=model_id,
            max_seq_length=max_seq_length,
            dtype=getattr(torch, dtype) if isinstance(dtype, str) else dtype,
            load_in_4bit=load_in_4bit,
            device_map=device_map,
        )

        if adapter:
            try:
                model.load_adapter(adapter)
                print(f"[UnslothModel] Loaded adapter: {adapter}")
            except Exception as e:
                print(f"[UnslothModel] WARNING: failed to load adapter '{adapter}': {e}")

        FastLanguageModel.for_inference(model)
        self.pipe = pipeline("text-generation", model=model, tokenizer=tokenizer)

    def _post(self, s: str) -> str:
        s = s.strip()
        for tag in ("<|eot_id|>", "<|end_of_text|>"):
            if tag in s:
                s = s.split(tag, 1)[0].strip()
        return s

    def generate(self, prompt: str, **kwargs) -> str:
        out = self.pipe(
            prompt,
            max_new_tokens=kwargs.get("max_new_tokens", self.max_new_tokens),
            **(self.gen_kwargs | kwargs),
        )[0]["generated_text"]
        # If someone passed in a chat template, still try to trim the assistant segment.
        if "<|start_header_id|>assistant<|end_header_id|>" in out:
            out = out.split("<|start_header_id|>assistant<|end_header_id|>")[-1]
        return self._post(out)

# -------------- llama.cpp (unused) ---------------
class LlamaCPP(BaseModel):
    def __init__(self, model_path: str, **kwargs):
        if Llama is None:
            raise RuntimeError("llama_cpp is not available in this environment.")
        self.llm = Llama(model_path=model_path, **kwargs)

    def generate(self, prompt: str, **kwargs) -> str:
        res = self.llm.create_chat_completion(
            messages=[{"role": "user", "content": prompt}],
            **kwargs,
        )
        return str(res["choices"][0]["message"]["content"]).strip()
