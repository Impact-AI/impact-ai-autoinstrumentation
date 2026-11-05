# SPDX-License-Identifier: Apache-2.0

"""Starts the ASGI fixture workload and ignores any option it does not know (the end-to-end suite passes a
`--api-key=...` argument, as many real services do, to show that arguments never leave the cluster)."""

import sys

import uvicorn

if __name__ == "__main__":
    unknown = [a for a in sys.argv[1:] if a.startswith("--")]
    print(f"ignoring {len(unknown)} option(s)", flush=True)
    uvicorn.run("app_asgi:app", host="0.0.0.0", port=8000)  # noqa: S104
