"""Henter og fryser data fra Energi Data Service til data/snapshot/ (bruges af evals og tests)."""
from ladeagent.data.eds import build_snapshot

if __name__ == "__main__":
    rows = build_snapshot()
    print("snapshot:", rows)
