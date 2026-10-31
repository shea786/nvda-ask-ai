@echo off
rem Fake Claude Code CLI for the Ask AI Wine smoke test. No network, no LLM, no Claude usage.
rem It is not an npm shim, so the add-on runs it through its cmd.exe fallback (quoteForCmd).
rem   claude auth status [--json]   prints signed-in JSON like Claude Code 2.1.314, exit 0
rem   claude auth logout            prints "Successfully logged out from your Anthropic account.", exit 0
rem   claude -p ...                 saves stdin to last-stdin.txt (via sort), prints a JSON result, exit 0.
rem   A prompt containing CLAUDE401 prints Claude Code's not-signed-in result (is_error true) and exits 1;
rem   CLAUDEFILE writes claude-report.md into the working directory (the project folder), as Claude's
rem   Write tool would.
rem Every call is appended to calls.log next to this file. The arguments are only logged, never scanned,
rem because "--tools" "" is an empty argument that would end a %%1 loop early.
setlocal EnableExtensions DisableDelayedExpansion
set "HERE=%~dp0"
>>"%HERE%calls.log" echo ARGS: %*
if /i "%~1"=="auth" if /i "%~2"=="status" (
	echo {"loggedIn": true, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "pro"}
	exit /b 0
)
if /i "%~1"=="auth" if /i "%~2"=="logout" (
	echo Successfully logged out from your Anthropic account.
	exit /b 0
)
if /i not "%~1"=="-p" (
	>&2 echo error: fake claude does not support: %*
	exit /b 2
)
sort > "%HERE%last-stdin.txt"
>>"%HERE%calls.log" echo CWD: %CD%
rem Only the new question counts ("Question: ..."); earlier turns ("You: ...") are in the prompt too.
findstr /c:"Question: CLAUDEFILE" "%HERE%last-stdin.txt" >nul && goto makefile
findstr /c:"Question: CLAUDE401" "%HERE%last-stdin.txt" >nul && goto fail401
goto answer
:makefile
>"%CD%\claude-report.md" echo # Report written by the fake Claude
goto answer
:fail401
echo {"type": "result", "subtype": "success", "is_error": true, "api_error_status": null, "terminal_reason": "api_error", "result": "Not logged in - Please run /login", "session_id": "smoke-401"}
exit /b 1
:answer
echo {"type": "result", "subtype": "success", "is_error": false, "result": "**Bonjour** from Claude.", "session_id": "smoke-session", "usage": {"input_tokens": 10, "output_tokens": 4}}
exit /b 0
