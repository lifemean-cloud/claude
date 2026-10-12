"""
매일 자동 실행 프로그램 (GitHub Actions에서 실행됨)

1. .agents/skills/ 의 taste-skill 디자인 규칙(SKILL.md)을 읽고
2. tasks/daily_task.md 의 지시 내용을 Claude에게 전달해
3. 세련된 HTML 결과물을 output/YYYY-MM-DD/index.html 로 저장하고
4. output/index.html 에 지난 결과물 목록(아카이브)을 갱신합니다.

필요한 것: 환경 변수 ANTHROPIC_API_KEY (GitHub 저장소 Secrets에 등록)
"""

import html
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic

# ===== 설정 (필요하면 여기만 바꾸세요) =====
ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = ROOT / ".agents" / "skills"
TASK_FILE = ROOT / "tasks" / "daily_task.md"
OUTPUT_DIR = ROOT / "output"

# 가상 컴퓨터가 함께 읽을 taste-skill 규칙 (폴더 이름)
SKILLS = [
    "design-taste-frontend",
    "high-end-visual-design",
    "full-output-enforcement",
]

MODEL = "claude-opus-5-5"
EFFORT = "high"
KST = ZoneInfo("Asia/Seoul")


def load_skills() -> str:
    """선택한 SKILL.md 파일들을 하나의 디자인 규칙 문서로 합칩니다."""
    parts = []
    for name in SKILLS:
        path = SKILLS_DIR / name / "SKILL.md"
        if not path.exists():
            print(f"[경고] 스킬 파일이 없어 건너뜁니다: {path}")
            continue
        parts.append(f'<skill name="{name}">\n{path.read_text(encoding="utf-8")}\n</skill>')
    if not parts:
        sys.exit("[오류] 읽을 수 있는 스킬 파일이 하나도 없습니다.")
    return "\n\n".join(parts)


def build_system_prompt(skills_text: str) -> str:
    return (
        "You are a senior front-end designer. Follow every design rule in the skills below "
        "when producing output.\n\n"
        f"<design_skills>\n{skills_text}\n</design_skills>\n\n"
        "Output requirements:\n"
        "- Return ONE complete, self-contained HTML document (inline CSS/JS) and nothing else.\n"
        "- The page is in Korean: set lang=\"ko\" and load a premium Korean-capable font such as "
        "Pretendard from cdn.jsdelivr.net, with sensible fallbacks.\n"
        "- It must look excellent on phones and desktops, and in both light and dark mode.\n"
        "- Do not wrap the document in Markdown code fences."
    )


def generate_html(client: anthropic.Anthropic, system_prompt: str, task: str, today: str) -> str:
    """Claude에게 결과물을 요청하고 HTML 문자열을 돌려받습니다."""
    with client.beta.messages.stream(
        model=MODEL,
        max_tokens=64000,
        thinking={"type": "adaptive"},
        output_config={"effort": EFFORT},
        # 안전 필터가 거절하면 서버가 알아서 다른 모델로 재시도합니다.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        # 매일 같은 스킬 규칙을 보내므로 캐시해서 비용을 줄입니다.
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": f"오늘 날짜: {today}\n\n{task}"}],
    ) as stream:
        response = stream.get_final_message()

    if response.stop_reason == "refusal":
        reason = response.stop_details.explanation if response.stop_details else "알 수 없음"
        sys.exit(f"[오류] 요청이 거절되었습니다: {reason}")
    if response.stop_reason == "max_tokens":
        sys.exit("[오류] 결과물이 너무 길어 중간에 잘렸습니다. tasks/daily_task.md 를 줄여 주세요.")

    text = "".join(block.text for block in response.content if block.type == "text").strip()
    # 혹시 ```html ... ``` 로 감싸서 왔다면 벗겨냅니다.
    fenced = re.search(r"```(?:html)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    if "<html" not in text.lower():
        sys.exit("[오류] HTML 문서를 받지 못했습니다.")

    u = response.usage
    print(
        f"[정보] 모델 {response.model} | 입력 {u.input_tokens} · 캐시 읽기 "
        f"{u.cache_read_input_tokens or 0} · 출력 {u.output_tokens} 토큰"
    )
    return text


def page_title(doc: str, fallback: str) -> str:
    match = re.search(r"<title>(.*?)</title>", doc, re.IGNORECASE | re.DOTALL)
    return html.unescape(match.group(1).strip()) if match else fallback


def write_archive() -> None:
    """output/index.html 에 날짜별 결과물 목록을 만듭니다."""
    entries = []
    for day_dir in sorted(OUTPUT_DIR.iterdir(), reverse=True):
        page = day_dir / "index.html"
        if day_dir.is_dir() and page.exists():
            title = page_title(page.read_text(encoding="utf-8"), day_dir.name)
            entries.append(
                f'<li><a href="{day_dir.name}/index.html">'
                f'<time>{day_dir.name}</time><span>{html.escape(title)}</span></a></li>'
            )

    archive = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>결과물 아카이브</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css">
<style>
  :root {{ --bg:#f7f6f3; --fg:#141414; --muted:#8a8780; --line:rgba(20,20,20,.08); --hover:#ffffff; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#0c0c0d; --fg:#ededec; --muted:#7c7b78; --line:rgba(255,255,255,.08); --hover:#161618; }}
  }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ background:var(--bg); color:var(--fg); font-family:Pretendard,system-ui,sans-serif;
         padding:clamp(48px,10vw,120px) 16px; -webkit-font-smoothing:antialiased; }}
  main {{ max-width:720px; margin:0 auto; }}
  h1 {{ font-size:clamp(32px,6vw,52px); font-weight:600; letter-spacing:-.03em; }}
  p {{ color:var(--muted); margin:12px 0 56px; }}
  ul {{ list-style:none; border-top:1px solid var(--line); }}
  a {{ display:flex; gap:24px; align-items:baseline; padding:20px 12px; color:inherit;
       text-decoration:none; border-bottom:1px solid var(--line); border-radius:12px;
       transition:background .4s cubic-bezier(.16,1,.3,1), transform .4s cubic-bezier(.16,1,.3,1); }}
  a:hover {{ background:var(--hover); transform:translateX(4px); }}
  time {{ color:var(--muted); font-variant-numeric:tabular-nums; font-size:14px; flex-shrink:0; }}
</style>
</head>
<body>
<main>
  <h1>결과물 아카이브</h1>
  <p>매일 자정 자동으로 만들어진 페이지 {len(entries)}개</p>
  <ul>
    {chr(10).join(entries)}
  </ul>
</main>
</body>
</html>
"""
    (OUTPUT_DIR / "index.html").write_text(archive, encoding="utf-8")


def main() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("[오류] ANTHROPIC_API_KEY 가 없습니다. GitHub 저장소 Secrets에 등록해 주세요.")

    today = datetime.now(KST).strftime("%Y-%m-%d")
    task = TASK_FILE.read_text(encoding="utf-8")
    system_prompt = build_system_prompt(load_skills())

    print(f"[정보] {today} 결과물 생성 시작 (스킬: {', '.join(SKILLS)})")
    doc = generate_html(anthropic.Anthropic(), system_prompt, task, today)

    day_dir = OUTPUT_DIR / today
    day_dir.mkdir(parents=True, exist_ok=True)
    (day_dir / "index.html").write_text(doc, encoding="utf-8")
    write_archive()
    print(f"[완료] output/{today}/index.html 저장")


if __name__ == "__main__":
    main()
