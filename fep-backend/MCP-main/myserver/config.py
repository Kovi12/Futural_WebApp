# MCP-main/myserver/config.py
from __future__ import annotations
import json, os
from pathlib import Path

class Config:
    def __init__(self, data):
        self.data = data
    @classmethod
    def from_json(cls, path):
        p = Path(path).expanduser().resolve()
        with p.open("r", encoding="utf-8") as fd:
            data = json.load(fd)
        return cls(data)
    def __getitem__(self, name):
        return self.data.get(name, None)

DEFAULT_CFG = Path(__file__).resolve().parents[1] / "config.json"
CONFIG_PATH = Path(os.environ.get("MCP_CONFIG", str(DEFAULT_CFG))).expanduser().resolve()

config = Config.from_json(CONFIG_PATH)
VAULT_PATH = Path(config["VAULT_PATH"]).expanduser().resolve()
