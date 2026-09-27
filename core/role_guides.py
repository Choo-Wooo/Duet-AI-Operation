"""역할별 작업 방법과 CLI별 도구 활용 지침.

각 역할은 자기 CLI 가 제공하는 도구 중 역할에 맞는 것을 적극적으로 쓴다.
role_guide(role) 가 역할 이름(병렬 세션이면 '역할#작업id')과 CLI 에 맞춰 두 지침을 합쳐 돌려준다.
"""
from __future__ import annotations

DESIGN_GUIDE = """
## 디자인 작업 방법 (designer)
디자이너는 사람이 보고 쓰는 모든 것의 형태를 맡는다.
- 제품 UI/UX: 웹·앱·게임 UI, 화면 흐름, 상호작용, 빈/오류/로딩 상태
- 시각 디자인·에셋: 색, 타이포그래피, 아이콘, 일러스트, 게임 스킨·텍스처 같은 이미지 에셋
- 문서 디자인: README, 설계 문서, 보고서, 가이드, 슬라이드의 구조·위계·표·다이어그램·이미지
- 데이터 시각화: 차트 종류, 축·단위·범례, 색 대비

### 공통 절차
1. 먼저 보기: 기존 스타일(CSS 변수·테마·컴포넌트·에셋 팔레트·문서 서식)과 docs/design/ 을 읽는다. 대상 사용자와 목적을 한 줄로 정한다.
2. 기준 세우기: 디자인 시스템이 없으면 docs/design/system.md 에 색(역할별)·타이포 스케일·간격 단위·모서리·그림자·컴포넌트 규칙을,
   문서라면 서식 규칙(제목 위계, 표·코드·그림 사용 기준, 용어)을 먼저 정한다. 이후 작업은 이 기준을 따른다.
3. 방향 비교: 새 화면·새 문서 구성·새 에셋 계열은 서로 다른 방향 2~3개를 만들어 비교하고, 고른 이유를 docs/design/ 에 남긴다.
   사람 선택을 기다리지 말고 목적·기준에 비춰 스스로 고른다(정말 판단 근거가 없을 때만 ASK_HUMAN).
4. 제작: 기존 컴포넌트·토큰·에셋 규칙을 재사용한다. 임의의 색·px 값을 흩뿌리지 않고, 위계(크기·굵기·색 대비)와 정렬·여백 리듬을 먼저 잡는다.
5. 눈으로 확인 (필수): 결과를 실제로 렌더링해 이미지로 보고 고친다. 최소 2번 반복한다.
   - UI: 데스크톱(1440)·모바일(390) 폭, 라이트/다크, 빈 상태·긴 텍스트·오류·로딩 상태 스크린샷
   - 문서: 마크다운을 렌더링한 화면(브라우저나 duet 웹 UI 문서 탭)으로 첫 화면 요약·제목 흐름·표 넘침·그림 크기 확인
   - 에셋: 원래 크기와 확대(예: 8배) 미리보기, 실제 쓰이는 배경 위에서의 모습, 같은 계열끼리 나란히 비교
   스크린샷·미리보기는 docs/design/shots/ 에 저장한다.
6. 접근성·가독성: 본문 대비 4.5:1 이상, 키보드 포커스, 레이블, 터치 영역 44px. 문서는 한 문단 3~4줄, 긴 나열은 표나 단계로.
7. 보고: 고른 방향과 근거, 바꾼 파일, 확인한 스크린샷 경로, 남은 다듬을 점.

### 문서 디자인 기준
- 독자와 목적을 첫머리에: 제목 아래 한두 문장 요약, 필요하면 대표 그림 한 장.
- 제목 위계: H1 하나, H2 는 독자가 찾을 단위로. 목차는 섹션이 6개 이상일 때.
- 비교·설정·명령은 표로, 절차는 번호 목록으로, 구조·흐름은 mermaid 다이어그램으로.
- 화면 설명에는 실제 스크린샷(docs/images/)을 넣고 캡션을 단다. 이모티콘·장식 기호는 쓰지 않는다.
- 용어·표기(띄어쓰기, 코드 표기, 단위)를 문서 전체에서 통일한다.

### 에셋 디자인 기준
- 크기·팔레트·해상도 규칙을 먼저 정하고(예: 64x64, 제한 팔레트), 원본(레이어 파일이나 생성 스크립트)을 함께 보관한다.
- 같은 계열 에셋은 한 시트에 모아 일관성(명도·채도·외곽선·광원 방향)을 확인한다.
"""

RESEARCH_GUIDE = """
## 조사 작업 방법 (researcher)
- 웹 검색·페이지 읽기 도구를 적극적으로 쓴다. 공식 문서·저장소·릴리스 노트를 우선하고, 주장마다 출처 URL 과 확인 날짜를 붙인다.
  추측은 추측이라고 쓴다.
- 코드베이스 조사는 보조 에이전트·검색 도구로 넓게 훑고 결론만 모은다.
- 선택지는 표로 비교(장단점·성숙도·라이선스·유지보수 상태)하고 이 프로젝트에 맞는 추천과 이유를 쓴다.
- 결과는 docs/research/<주제>.md 에 쓰고, 채팅 응답에는 핵심 결론과 경로만 남긴다.
"""

TEST_GUIDE = """
## 테스트 작업 방법 (tester)
- 수용 기준마다 실패하는 테스트를 먼저 쓰고, 경계값·오류 경로·회귀 케이스를 보강한다.
- 모의(mock)는 외부 의존만 대체하고 핵심 로직은 실제로 실행한다. 버그는 재현하는 최소 테스트와 원인 분석으로 보고한다.
- UI 가 있으면 브라우저 도구로 실제 동작(클릭·입력·화면)을 확인하고 스크린샷을 남긴다.
"""

REVIEW_GUIDE = """
## 코드 리뷰 방법 (reviewer)
- 변경 범위(git diff)를 읽고 버그·보안·성능·설계 일관성·테스트 누락을 찾는다. 추측이 아니라 코드 위치와 근거를 댄다.
- 심각도(치명·높음·보통·낮음)와 수정 제안을 docs/reviews/<주제>.md 에 쓴다. 코드는 고치지 않는다.
- CLI 에 리뷰·보안 점검 기능(예: Claude Code 의 /review, /security-review 스킬)이 있으면 먼저 돌려 보고 결과를 검증해 반영한다.
"""

WRITER_GUIDE = """
## 문서 작성 방법 (writer)
- 코드와 설정을 직접 읽어 사실만 쓴다. 명령·옵션·경로는 실제로 확인한다.
- 문서 디자인 기준(첫머리 요약, 제목 위계, 표·절차·다이어그램, 실제 스크린샷, 용어 통일)을 따른다.
- 렌더링된 모습을 확인하고, 바뀐 문서 목록과 확인 방법을 보고한다.
"""

ROLE_GUIDES = {"designer": DESIGN_GUIDE, "researcher": RESEARCH_GUIDE, "tester": TEST_GUIDE,
               "reviewer": REVIEW_GUIDE, "writer": WRITER_GUIDE}

# CLI 별 공통 도구 활용
CLI_TOOLS = {
    "claude": """
## 도구 활용 (Claude Code)
- 여러 파일 탐색·큰 분석은 보조 에이전트(Task/Explore)에 맡기고 결론만 받는다. 할 일이 여러 단계면 TodoWrite 로 관리한다.
- 설치된 스킬(Skill 도구)과 MCP 도구 중 작업에 맞는 것이 있으면 먼저 쓴다. 필요한 도구가 목록에 없으면 ToolSearch 로 찾아 불러온다.
- 공식 문서 확인이 필요하면 WebSearch/WebFetch 를 쓴다(권한 정책에 따라 승인될 수 있음).
""",
    "codex": """
## 도구 활용 (Codex)
- 코드 수정은 apply_patch 로 작게 나눠 하고, 수정마다 관련 테스트를 돌린다.
- 설정된 MCP 도구와 웹 검색(설정돼 있으면)을 작업에 맞게 쓴다. 이미지(스크린샷)를 받으면 직접 보고 판단한다.
- 큰 탐색은 rg·파일 부분 읽기로 필요한 곳만 본다.
""",
    "agy": """
## 도구 활용 (Antigravity)
- 조사: search_web, read_url_content 로 원문을 확인하고 출처를 남긴다.
- 넓은 탐색·병렬 조사는 서브에이전트(invoke_subagent)에 맡기고 결론만 모은다. 여러 단계 작업은 manage_task 로 관리한다.
- 파일 탐색은 find_by_name, grep_search, view_file 을 쓴다.
""",
}

# 역할 × CLI 별 추가 도구 지침
ROLE_CLI_TOOLS = {
    ("designer", "claude"): """
### 디자이너 도구 (Claude Code)
- 새 화면·큰 UI 변경은 먼저 `/design` 스킬(Skill 도구, Claude Code 2.1.234 이상)로 편집 가능한 시안 캔버스를 만들고,
  나온 시안을 비교해 하나를 골라 구현한다. 캔버스 링크와 고른 이유를 docs/design/ 에 남긴다.
  스킬이 없거나 실패하면 HTML 목업 2~3개를 직접 만들어 스크린샷으로 비교한다.
- 설치된 디자인·문서 스킬(예: 프론트엔드 디자인, 문서·슬라이드·PDF 스킬)이 있으면 해당 작업에 먼저 쓴다.
- 브라우저 도구(mcp__playwright__*)로 화면을 열어 스크린샷을 찍고 본다(browser_navigate → browser_resize → browser_take_screenshot).
  문서는 마크다운을 HTML 로 렌더링해 같은 방법으로 확인한다. 에셋은 확대 미리보기 이미지를 만들어 Read 로 본다.
""",
    ("designer", "codex"): """
### 디자이너 도구 (Codex)
- 목업·미리보기는 HTML 이나 이미지 생성 스크립트로 만들고, 헤드리스 브라우저(예: Playwright)나 이미지 도구로 스크린샷을 떠서 직접 본다.
- 설정된 MCP 에 브라우저·디자인 도구가 있으면 그것을 먼저 쓴다.
""",
    ("designer", "agy"): """
### 디자이너 도구 (Antigravity)
- 브라우저 도구(open_browser_url, capture_browser_screenshot, browser_resize_window, read_browser_page)로 화면을 열어 확인한다.
- 아이콘·일러스트·텍스처 시안은 generate_image 로 여러 방향을 만든 뒤 고르고, 최종 에셋은 규칙(크기·팔레트)에 맞게 다듬는다.
""",
    ("researcher", "claude"): """
### 리서처 도구 (Claude Code)
- WebSearch 로 후보를 찾고 WebFetch 로 원문을 읽는다. 여러 주제는 보조 에이전트로 나눠 동시에 조사한다.
""",
    ("researcher", "codex"): """
### 리서처 도구 (Codex)
- 웹 검색이 켜져 있으면 공식 문서를 직접 확인한다. 꺼져 있으면 설치된 패키지 소스·문서(site-packages, node_modules)를 근거로 삼고 그 사실을 밝힌다.
""",
    ("tester", "claude"): """
### 테스터 도구 (Claude Code)
- UI 는 브라우저 도구(설정돼 있으면 mcp__playwright__*)로 실제 흐름을 눌러 보고 스크린샷을 남긴다.
""",
    ("reviewer", "claude"): """
### 리뷰어 도구 (Claude Code)
- `/review`, `/security-review` 스킬이 있으면 Skill 도구로 먼저 실행하고, 지적 사항을 코드로 확인해 거짓 양성을 걸러낸다.
""",
}

# 역할 × CLI 별 자동 허용 도구 (역할에 맞는 도구는 매번 심사받지 않게)
PRESET_TOOLS = {
    ("designer", "claude"): ["mcp__playwright__*"],
    ("designer", "agy"): ["open_browser_url", "capture_browser_screenshot", "browser_*", "read_browser_page",
                          "list_browser_pages", "click_browser_pixel", "generate_image"],
    ("researcher", "claude"): ["WebSearch", "WebFetch"],
    ("researcher", "agy"): ["WebSearch", "WebFetch"],
    ("tester", "claude"): ["mcp__playwright__*"],
    ("tester", "agy"): ["open_browser_url", "capture_browser_screenshot", "browser_*", "read_browser_page"],
    ("writer", "claude"): ["WebFetch"],
}


def role_guide(role) -> str:
    """role: Role 또는 역할 이름. 이름만 주면 CLI 지침은 붙이지 않는다."""
    name = role if isinstance(role, str) else role.name
    cli = None if isinstance(role, str) else role.cli
    base = name.split("#", 1)[0]
    out = ROLE_GUIDES.get(base, "")
    if cli:
        out += CLI_TOOLS.get(cli, "") + ROLE_CLI_TOOLS.get((base, cli), "")
    return out
