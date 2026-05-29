#!/usr/bin/env python3
"""Populate dev_tokens.toml with existing keys from main .env."""
from pathlib import Path

env_keys = {}
with open(Path.home() / ".hermes" / ".env") as f:
    for line in f:
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            parts = line.split("=", 1)
            env_keys[parts[0]] = parts[1]

xi_key = env_keys.get("XIAOMI_API_KEY", "")
xi_url = env_keys.get("XIAOMI_BASE_URL", "https://token-plan-sgp.xiaomimimo.com/v1")
or_key = env_keys.get("OPENROUTER_API_KEY", "")

q = chr(34)  # double quote

lines = []
lines.append("# hermes-neo development tokens")
lines.append("# DO NOT COMMIT - in .gitignore")
lines.append("# Fill telegram.bot_token, then run: python3 load_tokens.py")
lines.append("")
lines.append("[telegram]")
lines.append("bot_token = " + q + q)
lines.append("allowed_users = " + q + "7769915285" + q)
lines.append("home_channel = " + q + "7769915285" + q)
lines.append("")
lines.append("[xiaomi]")
lines.append("api_key = " + q + xi_key + q)
lines.append("base_url = " + q + xi_url + q)
lines.append("")
lines.append("[openrouter]")
lines.append("api_key = " + q + or_key + q)
lines.append("")
lines.append("[camofox]")
lines.append("url = " + q + "http://localhost:9377" + q)
lines.append("")
lines.append("[tailscale]")
lines.append("auth_key = " + q + q)
lines.append("")

toml_path = Path(__file__).parent / "dev_tokens.toml"
with open(toml_path, "w") as f:
    f.write("\n".join(lines) + "\n")

print("TOML populated:")
print("  xiaomi.api_key: " + ("SET (" + str(len(xi_key)) + " chars)" if xi_key else "EMPTY"))
print("  openrouter.api_key: " + ("SET (" + str(len(or_key)) + " chars)" if or_key else "EMPTY"))
print("  telegram.bot_token: EMPTY - fill manually!")
print("")
print("Edit: " + str(toml_path))
