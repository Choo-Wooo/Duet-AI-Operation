# duet — Claude Code × Codex × Antigravity 오케스트레이터

설계자 에이전트와 대화하면, 설계자가 구현자(및 추가 역할)에게 일을 나눠 주고 결과를 검토하며 개발을 이어갑니다.
에이전트끼리는 프로젝트 루트의 `DIALOGUE.md` 한 문서로 대화하고, 각 CLI(Claude Code, Codex)는 자기 하네스(도구, CLAUDE.md/AGENTS.md, MCP, 스킬, 세션 기억)를 그대로 씁니다.

## 실행

```bash
# 1) 이 duet/ 폴더를 작업할 프로젝트 폴더 안에 복사
cp -r duet ~/my-project/

# 2) 프로젝트 폴더에서 실행
cd ~/my-project
python3 duet          # 터미널 분할 화면(TUI)
python3 duet --web    # 브라우저 웹 UI (권장)
```

`--web` 은 이 컴퓨터(127.0.0.1)에서만 열리는 웹 서버를 띄우고, 실행마다 새로 만든 토큰이 붙은 주소를 브라우저로 엽니다
(`--port 8765`, `--no-browser`). 종료는 터미널에서 Ctrl+C.

처음 실행하면 자동으로 다음을 준비합니다.

- `.duet/venv` 가상환경 + 의존성(claude-agent-sdk, textual, pyyaml). 파이썬 3.10+ 이 필요하며, 없으면 Homebrew 파이썬이나 `uv` 를 찾아 씁니다.
- 설치된 CLI 감지 → `.duet/roles.yaml` 생성
  - 설계자 Claude `claude-opus-5-5` · 구현자 Codex `gpt-6-astra`(effort medium, 병렬 최대 3)
  - 디자이너 Claude `claude-sonnet-5`(병렬 최대 2) · 리서처 Antigravity `gemini-3.8-flash-medium`(병렬 최대 2)
- `.duet/modes.yaml`, `.duet/policy.yaml`, `DIALOGUE.md`
- git 저장소가 아니면 `git init` (턴마다 스냅샷 커밋, `/rollback` 용). `.gitignore` 에 duet 폴더·venv·로그 추가

사전 조건: `claude`, `codex`, `agy`(Antigravity CLI) 중 쓰려는 CLI가 설치·로그인되어 있어야 합니다.
API 를 직접 부르지 않고 설치된 CLI 를 그대로 띄우므로 각 CLI 의 구독·설정·MCP 가 그대로 쓰입니다.

옵션: `--mode deliberate`, `--max-turns inf`, `--budget-usd 5`, `--max-hours 2`, `--no-tui`(단순 콘솔), `--fake`(CLI 없이 흐름 시험), `--new-session`, `--load <이름>`, `--list-saves`, `-m "첫 메시지"`

## 웹 UI (`--web`)

- **진행**: 왼쪽 대화 흐름(사람·턴 요약·지시문·승인·단계), 오른쪽 실시간 출력(역할 필터). 승인·선택 요청은 위에 카드로 뜹니다.
  아래 입력창의 대상 선택으로 메인·특정 역할·병렬 작업 세션에 직접 말할 수 있습니다.
- **작업 보드**: 병렬 작업 카드(대기·진행·완료·중단), 작업별 기록 보기, 재개·취소
- **질문**: 역할 세션을 복제한 읽기 전용 분신에게 프로젝트 질문 (본 작업에 영향 없음)
- **세션**: 지금 세션 저장, 이전 세션 목록에서 불러오기·삭제
- **역할·모델**: 로그인된 각 CLI 에서 조회한 모델 목록으로 역할의 CLI·모델·effort·권한·병렬 최대 세션 수·컨텍스트 한도 편집, 역할 추가·삭제
- **설정**: 병렬 동시 수, 자동 병합, 통합 테스트 명령 등

## 병렬 작업 (설계자가 세션 수를 정함)

설계자는 독립적으로 나눌 수 있는 일을 턴 끝의 `duet-work` 블록으로 여러 역할 세션에 동시에 맡깁니다.

```duet-work
- id: api-login
  role: implementer
  task: 로그인 API 와 테스트. 인터페이스는 docs/design/auth.md
- id: login-ui
  role: designer
  task: 로그인 화면 시안
  depends_on: [api-login]
```

- 작업마다 `.duet/worktrees/<id>` git 워크트리와 **로컬 전용** 브랜치 `duet/work/<id>` 를 만듭니다.
  출발점은 duet 을 시작할 때 체크아웃돼 있던 브랜치(예: Bitbucket 에서 배정받은 feature 브랜치)입니다.
- 작업 흐름: 계획(읽기 전용) ⇄ 설계자 분신 검토 → 구현 → duet 이 합의 테스트 실행 + 설계자 분신 검증
  → 기준 브랜치 최신 내용 병합(충돌은 작업자가 해결) → 통합 테스트 → 기준 브랜치에 squash 병합
  → 워크트리·작업 브랜치 삭제. 병합 커밋 작성자는 git 설정의 사용자 본인입니다.
- 설계자 분신: 설계자 세션을 복제해 작업마다 하나씩 붙으므로 설계자가 여러 세션과 동시에 협의합니다.
- 세션 간 협의: 작업자는 `CONSULT <작업id|architect> <질문>` 으로 다른 작업 세션(읽기 전용 복제본)이나 설계자에게 묻습니다.
- 동시 세션 수: 역할별 `max_sessions`(역할·모델 화면) 과 전체 `max_parallel`(기본 4) 안에서 설계자가 정합니다.
- push 는 사람만 합니다. duet 은 원격에 아무것도 올리지 않고, `duet/work/*` push 를 막는 pre-push 훅을 넣습니다
  (기존 pre-push 훅이 있으면 건드리지 않고 알려 줍니다). 모든 작업이 끝나면 기준 브랜치만 push 하면 됩니다.
- 작업 기록은 `docs/work/<id>.md`, 상태는 `.duet/work.json`. 명령: `/work`, `/work cancel <id>`, `/work resume <id>`, `/work msg <id> <메시지>`

## Antigravity CLI (agy)

- `agy -p … --output-format stream-json` 헤드리스로 턴마다 실행하고 `--conversation` 으로 대화를 이어갑니다.
- 헤드리스 agy 는 훅의 "허용"을 무시하는 버그가 있어, duet 은 agy 를 전체 허용으로 띄우고 작업 폴더의
  `.agents/hooks.json` PreToolUse 훅이 duet 권한 정책에 물어 **거부할 것만 막습니다**. 이 훅은 duet 이 띄운 agy 에서만
  동작하고(환경변수로 구분) 사람이 직접 쓰는 agy 에는 영향이 없습니다. 새로 만든 훅 파일은 `.git/info/exclude` 로 커밋에서 뺍니다.
- 모델은 `agy models` 의 slug 를 씁니다 (예: `gemini-3.8-flash-medium`; effort 가 이름에 들어 있음).

## 화면 (TUI)

- 왼쪽: 대화 흐름 (사람 메시지, 각 턴 요약, 위임/보고 지시문, 승인 판정)
- 오른쪽: 역할별 탭 — 실시간 메시지, 도구 호출, 명령 출력
- 아래: 상태 줄 (실행 중 역할, 턴/한도, 모드, 승인 대기, 비용·토큰, 경과 시간)과 입력창
- 승인 팝업: `Y` 허용 · `A` 세션 동안 허용 · `N` 거부(이유 입력 가능)
- `Ctrl+X` 현재 턴 중단 · `Ctrl+Q` 종료 · `Esc` 입력창으로

## 입력창 명령

| 명령 | 동작 |
| --- | --- |
| (일반 텍스트) | 설계자에게 전달 (진행 중이면 다음 턴 전에 끼워 넣음) |
| `/to <역할> <메시지>` | 특정 역할에게 직접 |
| `/mode [이름]` | sprint(20턴) · review(60턴) · deliberate(무제한) · modes.yaml 의 사용자 모드 |
| `/turns <N\|inf>` | 턴 한도 (inf = 무제한) |
| `/auto on\|off` | off 면 위임 전마다 확인 |
| `/role list\|add\|edit\|remove` | 역할 관리. 예) `/role add designer claude claude-sonnet-4-5 UI와 스타일 담당` |
| `/pause` `/resume` `/stop` | 멈춤 · 재개 · 현재 턴 중단 |
| `/rollback <턴번호>` | 그 턴의 git 스냅샷으로 되돌리기 |
| `/save [이름] [--force] [-- 메모]` | 대화 세션 저장. 같은 이름은 `--force`로 덮어쓰기 |
| `/saves` | 저장본 목록 (최신순) |
| `/load <이름>` | 현재 상태 자동 저장 후 저장본 불러오기 |
| `/save-delete <이름>` | 저장본 삭제 |
| `/status`, `/help`, `/quit` | |
| `/plan` | 활성 task의 계획서 경로·단계·제출/합의 버전·라운드 |

## 합의 후 구현

review·deliberate 및 사용자 모드는 기본적으로 합의 흐름을 사용합니다. sprint는 기존 위임 흐름입니다.
`.duet/modes.yaml`의 각 모드에 `agreement: true|false`로 설정합니다. 필드가 없는 기존 설정도 이 기본값을 사용합니다.
이미 진행 중인 task의 합의 규칙은 모드를 바꿔도 유지됩니다.

1. **plan**: 구현자는 읽기만 하고 계획 전문을 응답합니다. 요구 해석·설계와 다른 점·`files` 코드블록·AC·테스트와 AC의 대응·모의 범위·`test_command:`·열린 질문을 포함합니다. 계획서와 DIALOGUE는 오케스트레이터가 기록합니다.
2. **plan_review**: 메인이 해당 버전을 `AGREE vN` 또는 `REVISE 사유`로 검토합니다. 파일 목록과 테스트 명령이 없으면 합의할 수 없습니다.
3. **implement**: 합의 버전을 구현합니다. 벗어나야 하면 새 계획 전문과 `REPORT deviation 사유`를 응답합니다. 완료는 AC별 충족 표·테스트 매핑·명령과 출력/exit code를 포함한 `REPORT done`입니다.
4. **verify**: 메인이 계획 외 파일 변경과 테스트 방법을 검토하고 합의된 명령을 직접 실행한 뒤 `ACCEPT` 또는 `REWORK 사유`로 판단합니다.

지시문 형식은 `<!-- duet: 지시문 인자 -->`입니다. 코드블록과 인용 속 예제는 실행하지 않습니다.
중단·blocked·사람 질문 뒤에는 task를 보존합니다. 사람의 다음 메시지는 메인에게 전달되며,
대기 중 plan/implement는 메인의 `RESUME`, plan_review는 `AGREE`/`REVISE`, verify는 `ACCEPT`/`REWORK`로 이어집니다.
활성 task를 새 위임으로 덮어쓰지 않습니다. 취소하려면 메인이 `CANCEL 사유`를 냅니다.

`.duet/roles.yaml`의 settings에서 `plan_rounds: 3`, `plan_approval: architect`를 설정할 수 있습니다.
네 번째 검토 전에 사람에게 한 라운드 연장/대기/취소를 묻습니다. `plan_approval: human`이면 합의 후 사람의 최종 승인을 받습니다.

plan에서 Codex는 턴별 read-only sandbox를 사용합니다. Claude는 PreToolUse에서 Bash·쓰기·알 수 없는 도구를 거부하고
Read/Grep/Glob과 기존 정책에 따른 WebFetch/WebSearch만 사용합니다. 파일 지문은 gitignore와 무관하게 수집하며 런타임 캐시·세션 데이터·대화 archive는 제외합니다.
IDE 디렉터리 `.idea`·`.vscode`와 `.DS_Store` 파일도 기본 제외합니다.
`.duet/roles.yaml`의 `settings.fingerprint_exclude`에 파일·디렉터리 이름을 추가하면 모든 깊이에서 같은 이름을 제외합니다(경로/glob이 아닌 정확한 이름).
설정 기본값은 `.git`, `__pycache__`, `.pytest_cache`, `node_modules`, `DIALOGUE-archive`, `.idea`, `.vscode`, `.DS_Store`입니다.
목록을 비우거나 바꿔도 기본 제외와 `.duet/{venv,logs,saves,state.json}` 제외는 항상 유지됩니다. 예를 들어 다음 설정은 기본 제외에 두 이름을 더합니다.

```yaml
settings:
  fingerprint_exclude: [local-cache, local.log]
```

저장/불러오기에는 task와 합의 필드도 포함됩니다. 계획 문서 자체는 되돌리지 않고 합의 해시가 다르면 경고합니다.

개발 테스트 준비와 실행(프로젝트 루트):

```sh
.duet/venv/bin/python -m pip install -r duet/requirements-dev.txt
.duet/venv/bin/python -m pytest -p no:cacheprovider duet/tests -q
```

개발 의존성은 bootstrap의 런타임 requirements 해시에 포함하지 않습니다. SDK/RPC 모의 테스트와 실제 CLI 샌드박스 검증은 별도로 보고합니다.

## 세션 저장과 불러오기

`/save 작업전 -- 리팩터링 시작 전`으로 저장하고 `/load 작업전`으로 돌아올 수 있습니다.
이름을 생략하면 현재 시각으로 저장합니다. 턴 실행 중 `/save`는 거부되므로 `/pause` 후 현재 턴이 끝날 때까지 기다리세요.
`/load`는 진행 루프가 완전히 끝나야 가능합니다. 진행 중이면 `/stop` 후 종료를 기다리세요.

불러오기 전 현재 대화는 `_autosave-*`로 저장되며 최근 5개를 유지합니다. 불러온 뒤에는 사람의 다음 메시지를 기다리고
자동 위임을 끕니다(`/auto on`으로 다시 켤 수 있음). 현재 역할 설정은 유지하며, 없어진 역할이나 CLI가 바뀐 역할의 세션은 버립니다.
코드는 복원하지 않습니다. 저장 당시 git HEAD가 다르면 `/rollback` 안내를 표시합니다.

재시작하면서 복원하려면 `python3 duet --load 작업전`, 목록만 보려면 `python3 duet --list-saves`를 사용하세요.
`--load`와 `--new-session`은 함께 지정할 수 없습니다.
저장본에는 에이전트 세션 ID가 들어 있으므로 같은 컴퓨터의 원래 Claude/Codex 세션 데이터도 필요합니다.
지원되는 CLI에서는 저장 후 계속할 때와 불러온 뒤 처음 이어갈 때 세션을 분기합니다.

## 합의 기능을 끈 모드의 흐름

1. 사람 메시지 → 설계자 턴. 설계자는 `DIALOGUE.md` 끝에 `## [architect] #N` 턴을 쓰고, 위임하려면 `<!-- duet: DELEGATE implementer -->` + `<!-- duet: TASK ... -->` 를 남깁니다.
2. 구현자 턴 → `<!-- duet: REPORT done -->` 로 보고 → 설계자가 검토 → 다시 위임하거나 `<!-- duet: STATUS done -->`.
3. `ASK_HUMAN` 은 자동 진행을 멈추고 사람에게 묻고, `PROPOSE_ROLE` 은 새 역할 추가를 제안합니다(사람 승인).

턴 한도에 닿으면 "10/30턴 더 · 무제한 · 설계자가 정리 · 멈추기" 중에 고릅니다. 무제한일 때는 정체(최근 N턴 동안 코드·문서 변경 없음), 핑퐁(변경을 서로 되돌림), 같은 작업 3회 반복을 감지하면 멈추고 묻습니다. 50턴마다 설계자가 체크포인트 요약을 쓰고 이전 대화는 `DIALOGUE-archive/` 로 옮깁니다.

## 권한 3단계

- **자동 허용**: 읽기·검색, 프로젝트 안 파일 수정, 테스트·린트·빌드 명령
- **설계자 판단**: 패키지 설치, 목록에 없는 명령, 웹 조회 → 설계자의 심사 전용 세션이 JSON으로 허가/거부/사람에게 넘김
- **사람 확인**: `rm -rf`, `git push`/`reset --hard`, `sudo`, `curl`/`ssh`, `.env`·키 파일, 프로젝트 밖 경로 (설계자 의견과 함께 팝업)

규칙은 `.duet/policy.yaml` 에서 고칩니다. 판정 기록은 `DIALOGUE.md` 에 `> [approval] …` 로 남습니다.
일반 턴의 Claude는 사용자 설정(`~/.claude/settings.json`)의 allow 규칙이 먼저 적용됩니다. plan 도구 제한과 verify Bash 검증은 PreToolUse에서 먼저 적용합니다.
일반 턴의 Codex는 역할의 샌드박스 + `untrusted` 승인 정책으로 신뢰 목록 밖 명령만 duet으로 넘기며, plan 턴은 별도 read-only 정책을 사용합니다.

## 파일

```
duet/                 이 도구 (프로젝트에 복사)
.duet/roles.yaml      역할 (main, cli, model, brief, permissions, effort)
.duet/modes.yaml      대화 모드 프리셋
.duet/policy.yaml     권한 정책
.duet/state.json      세션 ID, 턴 번호, 현재 모드 (자동 관리)
.duet/saves/<이름>/   대화·진행 상태·역할 설정 사본과 저장 메타데이터
.duet/logs/*.jsonl    모든 이벤트 로그
DIALOGUE.md           에이전트 공유 대화 문서
```

## 참고

- 턴마다 각 CLI의 기본 시스템 프롬프트와 컨텍스트가 실리므로, 작은 작업도 턴 수만큼 토큰이 듭니다. 상태 줄의 비용(Claude)과 토큰(Codex)을 확인하세요.
- Codex app-server 프로토콜은 버전마다 바뀔 수 있습니다. 문제가 생기면 `codex --version` 과 `.duet/logs` 를 확인하세요.
