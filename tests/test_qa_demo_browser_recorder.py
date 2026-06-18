from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "qa_demo_recorder.mjs"


def test_browser_recorder_rejects_click_navigation_outside_preview_origin(
    tmp_path: Path,
) -> None:
    module_root = tmp_path / "modules"
    playwright_dir = module_root / "playwright"
    playwright_dir.mkdir(parents=True)
    video_path = tmp_path / "source-video.webm"
    video_path.write_bytes(b"\x1a\x45\xdf\xa3" + (b"0" * 4096))
    (playwright_dir / "index.js").write_text(
        f"""
let currentUrl = "";
const videoPath = {json.dumps(str(video_path))};

function locator(selector) {{
  return {{
    async click() {{
      if (selector === "#external-link") {{
        currentUrl = "https://evil.example/fake-proof";
      }}
    }},
    async fill() {{}},
    async press() {{}},
    async selectOption() {{}},
    async waitFor() {{}},
    async textContent() {{
      return "Feature is visible";
    }},
  }};
}}

const page = {{
  async goto(url) {{
    currentUrl = url;
  }},
  url() {{
    return currentUrl;
  }},
  video() {{
    return {{
      async path() {{
        return videoPath;
      }},
    }};
  }},
  getByText() {{
    return {{
      async count() {{
        return 0;
      }},
    }};
  }},
  locator,
  async waitForURL(url) {{
    currentUrl = url;
  }},
}};

module.exports = {{
  chromium: {{
    async launch() {{
      return {{
        async newContext() {{
          return {{
            async newPage() {{
              return page;
            }},
            async close() {{}},
          }};
        }},
        async close() {{}},
      }};
    }},
  }},
}};
""",
        encoding="utf-8",
    )
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "output.json"
    output_dir = tmp_path / "videos"
    input_path.write_text(
        json.dumps(
            {
                "preview_url": "https://preview.example/",
                "output_dir": str(output_dir),
                "scenarios": [
                    {
                        "name": "External click",
                        "start_path": "/",
                        "steps": [
                            {"action": "click", "selector": "#external-link"},
                            {
                                "action": "assert_visible",
                                "selector": "text=Feature is visible",
                            },
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["QA_DEMO_PLAYWRIGHT_MODULE_DIR"] = str(module_root)
    result = subprocess.run(
        ["node", str(SCRIPT_PATH), str(input_path), str(output_path)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    output = json.loads(output_path.read_text(encoding="utf-8"))
    assert output["recordings"] == []
    assert "QA demo browser scenario must stay on preview release origin" in output["failure_evidence"][0]["error_message"]


def test_browser_recorder_writes_failure_evidence_for_app_load_error(tmp_path: Path) -> None:
    module_root = tmp_path / "modules"
    playwright_dir = module_root / "playwright"
    playwright_dir.mkdir(parents=True)
    video_path = tmp_path / "source-video.webm"
    video_path.write_bytes(b"\x1a\x45\xdf\xa3" + (b"0" * 4096))
    (playwright_dir / "index.js").write_text(
        f"""
let currentUrl = "";
const handlers = {{}};
const videoPath = {json.dumps(str(video_path))};

function locator() {{
  return {{
    async click() {{}},
    async fill() {{}},
    async press() {{}},
    async selectOption() {{}},
    async waitFor() {{
      throw new Error("QA Demo Ready was not visible");
    }},
    async textContent() {{
      return "";
    }},
  }};
}}

const page = {{
  on(event, handler) {{
    handlers[event] = handler;
  }},
  async goto(url) {{
    currentUrl = url;
    if (handlers.pageerror) {{
      handlers.pageerror(new Error("process is not defined"));
    }}
  }},
  url() {{
    return currentUrl;
  }},
  video() {{
    return {{
      async path() {{
        return videoPath;
      }},
    }};
  }},
  getByText() {{
    return {{
      async count() {{
        return 0;
      }},
    }};
  }},
  locator,
  async waitForURL(url) {{
    currentUrl = url;
  }},
}};

module.exports = {{
  chromium: {{
    async launch() {{
      return {{
        async newContext() {{
          return {{
            async newPage() {{
              return page;
            }},
            async close() {{}},
          }};
        }},
        async close() {{}},
      }};
    }},
  }},
}};
""",
        encoding="utf-8",
    )
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "output.json"
    output_dir = tmp_path / "videos"
    input_path.write_text(
        json.dumps(
            {
                "preview_url": "https://preview.example/",
                "output_dir": str(output_dir),
                "scenarios": [
                    {
                        "name": "App load",
                        "start_path": "/",
                        "steps": [{"action": "assert_visible", "selector": "text=QA Demo Ready"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["QA_DEMO_PLAYWRIGHT_MODULE_DIR"] = str(module_root)
    result = subprocess.run(
        ["node", str(SCRIPT_PATH), str(input_path), str(output_path)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    output = json.loads(output_path.read_text(encoding="utf-8"))
    assert output["recordings"] == []
    assert output["failure_evidence"][0]["name"] == "App load"
    assert "process is not defined" in output["failure_evidence"][0]["error_message"]
    assert "QA Demo Ready was not visible" in output["failure_evidence"][0]["error_message"]
    assert Path(output["failure_evidence"][0]["path"]).exists()


def test_browser_recorder_includes_failed_http_response_in_load_failure_evidence(tmp_path: Path) -> None:
    module_root = tmp_path / "modules"
    playwright_dir = module_root / "playwright"
    playwright_dir.mkdir(parents=True)
    video_path = tmp_path / "source-video.webm"
    video_path.write_bytes(b"\x1a\x45\xdf\xa3" + (b"0" * 4096))
    (playwright_dir / "index.js").write_text(
        f"""
let currentUrl = "";
const handlers = {{}};
const videoPath = {json.dumps(str(video_path))};

function locator() {{
  return {{
    async click() {{}},
    async fill() {{}},
    async press() {{}},
    async selectOption() {{}},
    async waitFor() {{
      throw new Error("QA Demo Ready was not visible");
    }},
    async textContent() {{
      return "";
    }},
  }};
}}

const page = {{
  on(event, handler) {{
    handlers[event] = handler;
  }},
  async goto(url) {{
    currentUrl = url;
    if (handlers.response) {{
      handlers.response({{
        status() {{
          return 500;
        }},
        url() {{
          return url;
        }},
        request() {{
          return {{
            method() {{
              return "GET";
            }},
          }};
        }},
      }});
    }}
  }},
  url() {{
    return currentUrl;
  }},
  video() {{
    return {{
      async path() {{
        return videoPath;
      }},
    }};
  }},
  getByText() {{
    return {{
      async count() {{
        return 0;
      }},
    }};
  }},
  locator,
  async waitForURL(url) {{
    currentUrl = url;
  }},
}};

module.exports = {{
  chromium: {{
    async launch() {{
      return {{
        async newContext() {{
          return {{
            async newPage() {{
              return page;
            }},
            async close() {{}},
          }};
        }},
        async close() {{}},
      }};
    }},
  }},
}};
""",
        encoding="utf-8",
    )
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "output.json"
    output_dir = tmp_path / "videos"
    input_path.write_text(
        json.dumps(
            {
                "preview_url": "https://preview.example/",
                "output_dir": str(output_dir),
                "scenarios": [
                    {
                        "name": "App load",
                        "start_path": "/",
                        "steps": [{"action": "assert_visible", "selector": "text=QA Demo Ready"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["QA_DEMO_PLAYWRIGHT_MODULE_DIR"] = str(module_root)
    result = subprocess.run(
        ["node", str(SCRIPT_PATH), str(input_path), str(output_path)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    output = json.loads(output_path.read_text(encoding="utf-8"))
    assert "http 500 GET https://preview.example/" in output["failure_evidence"][0]["error_message"]


def test_browser_recorder_routes_public_host_to_recording_host_without_host_header(tmp_path: Path) -> None:
    module_root = tmp_path / "modules"
    playwright_dir = module_root / "playwright"
    playwright_dir.mkdir(parents=True)
    video_path = tmp_path / "source-video.webm"
    capture_path = tmp_path / "capture.json"
    video_path.write_bytes(b"\x1a\x45\xdf\xa3" + (b"0" * 4096))
    (playwright_dir / "index.js").write_text(
        f"""
const fs = require("node:fs");
let currentUrl = "";
const state = {{}};
const videoPath = {json.dumps(str(video_path))};
const capturePath = {json.dumps(str(capture_path))};

function locator() {{
  return {{
    async click() {{}},
    async fill() {{}},
    async press() {{}},
    async selectOption() {{}},
    async waitFor() {{}},
    async textContent() {{
      return "Feature is visible";
    }},
  }};
}}

const page = {{
  async goto(url) {{
    currentUrl = url;
    state.gotoUrl = url;
  }},
  url() {{
    return currentUrl;
  }},
  video() {{
    return {{
      async path() {{
        return videoPath;
      }},
    }};
  }},
  getByText() {{
    return {{
      async count() {{
        return 0;
      }},
    }};
  }},
  locator,
  async waitForURL(url) {{
    currentUrl = url;
  }},
}};

module.exports = {{
  chromium: {{
    async launch(options) {{
      state.launchOptions = options;
      return {{
        async newContext(options) {{
          state.options = options;
          return {{
            async newPage() {{
              return page;
            }},
            async close() {{
              fs.writeFileSync(capturePath, JSON.stringify(state));
            }},
          }};
        }},
        async close() {{}},
      }};
    }},
  }},
}};
""",
        encoding="utf-8",
    )
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "output.json"
    output_dir = tmp_path / "videos"
    input_path.write_text(
        json.dumps(
            {
                "preview_url": "http://preview.example:8088/",
                "recording_url": "http://127.0.0.1:8088/",
                "recording_host_header": "preview.example",
                "output_dir": str(output_dir),
                "scenarios": [
                    {
                        "name": "Internal route",
                        "start_path": "/feature?variant=1",
                        "steps": [
                            {
                                "action": "assert_visible",
                                "selector": "text=Feature is visible",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["QA_DEMO_PLAYWRIGHT_MODULE_DIR"] = str(module_root)
    result = subprocess.run(
        ["node", str(SCRIPT_PATH), str(input_path), str(output_path)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    output = json.loads(output_path.read_text(encoding="utf-8"))
    assert output["recordings"][0]["name"] == "Internal route"
    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    assert capture["gotoUrl"] == "http://preview.example:8088/feature?variant=1"
    assert capture["launchOptions"]["args"] == [
        "--host-resolver-rules=MAP preview.example 127.0.0.1"
    ]
    assert "extraHTTPHeaders" not in capture["options"]
