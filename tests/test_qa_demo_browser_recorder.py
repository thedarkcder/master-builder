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
    assert "QA demo browser scenario must stay on preview release origin" in (
        result.stderr + result.stdout
    )
    assert not output_path.exists()
