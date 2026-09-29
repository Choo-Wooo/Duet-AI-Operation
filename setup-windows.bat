@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion
rem ======================================================================
rem  duet Windows 환경 점검·설치
rem
rem    setup-windows.bat          점검하고, 빠진 것은 하나씩 물어본 뒤 설치
rem    setup-windows.bat /check   점검만 (설치하지 않음)
rem    setup-windows.bat /yes     묻지 않고 빠진 것을 모두 설치
rem
rem  점검 항목: Python 3.10+, Git(2.45+ 권장)과 Git Bash, Node.js,
rem            claude / codex / agy CLI 설치와 로그인, duet 가상환경(.duet\venv)
rem ======================================================================

set "MODE=ask"
if /i "%~1"=="/check" set "MODE=check"
if /i "%~1"=="/yes" set "MODE=yes"

set "DUET_DIR=%~dp0"
if "%DUET_DIR:~-1%"=="\" set "DUET_DIR=%DUET_DIR:~0,-1%"
for %%I in ("%DUET_DIR%\..") do set "PROJECT_DIR=%%~fI"

set /a FAIL=0
set /a WARN=0
set /a CLI_COUNT=0
set "HAS_WINGET="
where winget >nul 2>&1 && set "HAS_WINGET=1"

echo.
echo  duet Windows 환경 점검
echo  duet 폴더   : %DUET_DIR%
echo  프로젝트    : %PROJECT_DIR%
if "%MODE%"=="check" echo  모드        : 점검만
if "%MODE%"=="yes" echo  모드        : 빠진 것 모두 자동 설치
echo.

rem ---------------------------------------------------------------- Python
echo [1/6] Python 3.10 이상
call :find_python
if not defined PY (
    call :ask "Python 3.12 를 설치할까요? winget"
    if "!ANSWER!"=="Y" (
        call :winget_install Python.Python.3.12
        call :refresh_path
        call :find_python
    )
)
if defined PY (
    call :ok "Python !PYVER!  - !PY!"
) else (
    call :fail "Python 3.10 이상이 없습니다. https://www.python.org/downloads/windows/ 에서 설치하세요."
)

rem ---------------------------------------------------------------- Git
echo [2/6] Git
call :find_git
if not defined GITVER (
    call :ask "Git 을 설치할까요? winget"
    if "!ANSWER!"=="Y" (
        call :winget_install Git.Git
        call :refresh_path
        call :find_git
    )
) else if !GITNUM! LSS 245 (
    call :warn "Git !GITVER! - 하위 폴더 프로젝트의 병렬 작업에는 2.45 이상이 필요합니다."
    call :ask "Git 을 최신 버전으로 올릴까요? winget"
    if "!ANSWER!"=="Y" (
        call :winget_upgrade Git.Git
        call :refresh_path
        call :find_git
    )
)
if defined GITVER (
    if !GITNUM! GEQ 245 call :ok "Git !GITVER!"
) else (
    call :fail "Git 이 없습니다. https://git-scm.com/download/win 에서 설치하세요."
)
call :find_bash
if defined GITBASH (
    call :ok "Git Bash  - !GITBASH!"
) else (
    call :warn "Git Bash 를 찾지 못했습니다. 합의 테스트 명령을 bash 대신 cmd 로 실행합니다."
)

rem ---------------------------------------------------------------- Node.js
echo [3/6] Node.js  - npm 으로 CLI 설치, 디자이너의 브라우저 도구 npx
call :find_node
if not defined NODEVER (
    call :ask "Node.js LTS 를 설치할까요? winget"
    if "!ANSWER!"=="Y" (
        call :winget_install OpenJS.NodeJS.LTS
        call :refresh_path
        call :find_node
    )
)
if defined NODEVER (
    call :ok "Node.js !NODEVER!"
) else (
    call :warn "Node.js 가 없습니다. 디자이너의 브라우저 도구와 npm 설치를 쓸 수 없습니다. https://nodejs.org"
)

rem ---------------------------------------------------------------- CLI
echo [4/6] 에이전트 CLI  - 하나 이상 필요
call :check_claude
call :check_codex
call :check_agy
if !CLI_COUNT! EQU 0 call :fail "claude / codex / agy 중 설치된 CLI 가 없습니다."

rem ---------------------------------------------------------------- 가상환경
echo [5/6] duet 가상환경  - %PROJECT_DIR%\.duet\venv
set "VPY=%PROJECT_DIR%\.duet\venv\Scripts\python.exe"
if exist "%VPY%" if exist "%PROJECT_DIR%\.duet\venv\pyvenv.cfg" (
    call :ok "준비됨"
    goto :venv_done
)
if not defined PY (
    call :fail "Python 이 없어 가상환경을 만들 수 없습니다."
    goto :venv_done
)
call :ask "가상환경을 만들고 의존성을 설치할까요? 1~2분"
if not "!ANSWER!"=="Y" (
    call :warn "아직 없습니다. 처음 실행할 때 자동으로 만듭니다."
    goto :venv_done
)
pushd "%PROJECT_DIR%"
!PY! "%DUET_DIR%" --list-saves >nul
set "RC=!ERRORLEVEL!"
popd
if exist "%VPY%" (
    call :ok "가상환경을 만들었습니다."
) else (
    call :fail "가상환경을 만들지 못했습니다. 종료 코드 !RC!"
)
:venv_done

rem ---------------------------------------------------------------- 요약
echo [6/6] 요약
echo.
if !FAIL! GTR 0 (
    echo   문제 !FAIL!개, 주의 !WARN!개 - 위의 [X] 항목을 해결한 뒤 다시 실행하세요.
) else (
    echo   필수 항목 모두 준비됨. 주의 !WARN!개
    echo.
    echo   프로젝트 폴더에서 실행:
    echo     cd /d "%PROJECT_DIR%"
    for %%N in ("%DUET_DIR%") do echo     !PY! %%~nxN --web
)
echo.
set "EXITCODE=0"
if !FAIL! GTR 0 set "EXITCODE=1"
call :maybe_pause
exit /b %EXITCODE%


rem ======================================================================
rem  서브루틴
rem ======================================================================
:ok
echo   [OK] %~1
exit /b 0

:warn
echo   [주의] %~1
set /a WARN+=1
exit /b 0

:fail
echo   [X]  %~1
set /a FAIL+=1
exit /b 0

:ask
rem  %1 질문 → ANSWER=Y/N
if "%MODE%"=="check" (set "ANSWER=N" & exit /b 0)
if "%MODE%"=="yes" (set "ANSWER=Y" & exit /b 0)
choice /c YN /n /m "      %~1 [Y/N] "
if errorlevel 2 (set "ANSWER=N") else (set "ANSWER=Y")
exit /b 0

:winget_install
if not defined HAS_WINGET (
    echo       winget 이 없어 자동 설치할 수 없습니다. 직접 설치하세요.
    exit /b 1
)
winget install -e --id %~1 --accept-source-agreements --accept-package-agreements
exit /b %ERRORLEVEL%

:winget_upgrade
if not defined HAS_WINGET (
    echo       winget 이 없어 자동 업그레이드할 수 없습니다. 직접 설치하세요.
    exit /b 1
)
winget upgrade -e --id %~1 --accept-source-agreements --accept-package-agreements
exit /b %ERRORLEVEL%

:refresh_path
rem  방금 설치한 프로그램이 보이도록 레지스트리의 PATH 를 다시 읽는다
for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "[Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')"`) do set "PATH=%%P"
exit /b 0

:find_python
set "PY="
set "PYVER="
for %%V in (3.13 3.12 3.11 3.10) do (
    if not defined PY (
        py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V"
    )
)
if not defined PY (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "PY=python"
)
if not defined PY exit /b 0
for /f "delims=" %%v in ('!PY! -c "import platform; print(platform.python_version())"') do set "PYVER=%%v"
exit /b 0

:find_git
set "GITVER="
set /a GITNUM=0
for /f "tokens=3" %%v in ('git --version 2^>nul') do set "GITVER=%%v"
if not defined GITVER exit /b 0
for /f "tokens=1,2 delims=." %%a in ("!GITVER!") do set /a GITNUM=%%a*100+%%b
exit /b 0

:find_bash
set "GITBASH="
if defined CLAUDE_CODE_GIT_BASH_PATH if exist "%CLAUDE_CODE_GIT_BASH_PATH%" set "GITBASH=%CLAUDE_CODE_GIT_BASH_PATH%"
for /f "delims=" %%g in ('where git 2^>nul') do (
    if not defined GITBASH (
        for %%r in ("%%~dpg..") do if exist "%%~fr\bin\bash.exe" set "GITBASH=%%~fr\bin\bash.exe"
        for %%r in ("%%~dpg..\..") do if exist "%%~fr\bin\bash.exe" if not defined GITBASH set "GITBASH=%%~fr\bin\bash.exe"
    )
)
if not defined GITBASH if exist "%ProgramFiles%\Git\bin\bash.exe" set "GITBASH=%ProgramFiles%\Git\bin\bash.exe"
exit /b 0

:find_node
set "NODEVER="
for /f "delims=" %%v in ('node --version 2^>nul') do set "NODEVER=%%v"
exit /b 0

:locate
rem  %1 이름, %2 결과 변수, %3.. 추가로 볼 경로
set "%~2="
for /f "delims=" %%p in ('where %~1 2^>nul') do if not defined %~2 set "%~2=%%p"
:locate_more
if "%~3"=="" exit /b 0
if not defined %~2 if exist "%~3" set "%~2=%~3"
shift /3
goto :locate_more

:check_claude
call :locate claude CLAUDE_EXE "%USERPROFILE%\.local\bin\claude.exe"
if not defined CLAUDE_EXE (
    call :ask "Claude Code 를 설치할까요? 공식 설치 스크립트"
    if "!ANSWER!"=="Y" (
        powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://claude.ai/install.ps1 | iex"
        call :refresh_path
        call :locate claude CLAUDE_EXE "%USERPROFILE%\.local\bin\claude.exe"
    )
)
if not defined CLAUDE_EXE (
    call :warn "claude 없음 - Claude Code 역할을 쓸 수 없습니다. https://claude.com/claude-code"
    exit /b 0
)
set /a CLI_COUNT+=1
set "V="
for /f "delims=" %%v in ('"!CLAUDE_EXE!" --version 2^>nul') do if not defined V set "V=%%v"
"!CLAUDE_EXE!" auth status 2>nul | findstr /r /c:"loggedIn.: true" >nul
if errorlevel 1 (
    call :warn "claude !V! - 로그인 안 됨. 터미널에서 claude 를 실행해 /login 하세요."
) else (
    call :ok "claude !V! - 로그인됨"
)
exit /b 0

:check_codex
call :locate codex CODEX_EXE "%LOCALAPPDATA%\Programs\OpenAI\Codex\bin\codex.exe"
if not defined CODEX_EXE (
    if defined NODEVER (
        call :ask "Codex CLI 를 설치할까요? npm i -g @openai/codex"
        if "!ANSWER!"=="Y" (
            call npm i -g @openai/codex
            call :refresh_path
            call :locate codex CODEX_EXE "%LOCALAPPDATA%\Programs\OpenAI\Codex\bin\codex.exe" "%APPDATA%\npm\codex.cmd"
        )
    )
)
if not defined CODEX_EXE (
    call :warn "codex 없음 - Codex 역할을 쓸 수 없습니다. Node.js 설치 후 npm i -g @openai/codex"
    exit /b 0
)
set /a CLI_COUNT+=1
set "V="
for /f "delims=" %%v in ('"!CODEX_EXE!" --version 2^>nul') do if not defined V set "V=%%v"
"!CODEX_EXE!" login status 2>&1 | findstr /b /c:"Logged in" >nul
if errorlevel 1 (
    call :warn "!V! - 로그인 안 됨. 터미널에서 codex login 을 실행하세요."
) else (
    call :ok "!V! - 로그인됨"
)
exit /b 0

:check_agy
call :locate agy AGY_EXE "%LOCALAPPDATA%\agy\bin\agy.exe"
if not defined AGY_EXE (
    call :warn "agy 없음 - Antigravity 역할을 쓸 수 없습니다. https://antigravity.google 안내에 따라 설치하세요."
    exit /b 0
)
set /a CLI_COUNT+=1
set "V="
for /f "delims=" %%v in ('"!AGY_EXE!" --version 2^>nul') do if not defined V set "V=%%v"
"!AGY_EXE!" models <nul >nul 2>&1
if errorlevel 1 (
    call :warn "agy !V! - 로그인 안 됨. 터미널에서 agy 를 실행해 로그인하세요."
) else (
    call :ok "agy !V! - 로그인됨"
)
exit /b 0

:maybe_pause
rem  탐색기에서 더블클릭으로 실행했을 때만 창이 바로 닫히지 않게 멈춘다
if defined DUET_NO_PAUSE exit /b 0
echo %cmdcmdline% | find /i "%~nx0" >nul && pause
exit /b 0
