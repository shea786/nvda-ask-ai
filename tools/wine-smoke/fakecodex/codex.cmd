@echo off
rem Fake OpenAI Codex CLI for the Ask GPT Wine smoke test. No network, no LLM, no ChatGPT usage.
rem It is not an npm shim, so the add-on runs it through its cmd.exe fallback (quoteForCmd).
rem   codex login status       prints "Logged in using ChatGPT", exit 0
rem   codex logout             prints "Successfully logged out", exit 0
rem   codex exec ... -o F -    saves stdin to last-stdin.txt (via sort: Wine has no more.exe and
rem                            its findstr has no regex), writes the answer to F and stdout, exit 0.
rem   A prompt containing SLOW waits about 24 s first, to test cancel and the Job Object kill;
rem   MEDIUM waits about 3 s; FAIL401 prints a Codex-style 401 error on stderr and exits 1.
rem Every call is appended to calls.log next to this file.
setlocal EnableExtensions DisableDelayedExpansion
set "HERE=%~dp0"
>>"%HERE%calls.log" echo ARGS: %*
if /i "%~1"=="login" if /i "%~2"=="status" (
	echo Logged in using ChatGPT
	exit /b 0
)
if /i "%~1"=="logout" (
	echo Successfully logged out
	exit /b 0
)
if /i not "%~1"=="exec" (
	>&2 echo ERROR: fake codex does not support: %*
	exit /b 2
)
set "OFILE="
:scan
if "%~1"=="" goto scanned
if "%~1"=="-o" set "OFILE=%~2"
shift
goto scan
:scanned
sort > "%HERE%last-stdin.txt"
>>"%HERE%calls.log" echo OFILE: %OFILE%
findstr /c:SLOW "%HERE%last-stdin.txt" >nul && goto slow
findstr /c:FAIL401 "%HERE%last-stdin.txt" >nul && goto fail401
findstr /c:MEDIUM "%HERE%last-stdin.txt" >nul && goto medium
goto answer
:slow
>>"%HERE%calls.log" echo SLOW-START
ping -n 25 127.0.0.1 >nul
>>"%HERE%calls.log" echo SLOW-FINISHED
goto answer
:fail401
>&2 echo ERROR: unexpected status 401 Unauthorized: Missing bearer or basic authentication in header
exit /b 1
:medium
ping -n 4 127.0.0.1 >nul
:answer
if defined OFILE >"%OFILE%" echo **Paris** is the capital of France.
echo **Paris** is the capital of France.
exit /b 0
