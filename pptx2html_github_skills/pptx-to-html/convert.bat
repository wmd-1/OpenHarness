@echo off
REM ============================================================================
REM PPTX to HTML Converter - Batch File (Windows)
REM 사용법: convert.bat <input.pptx> [output_dir] [dpi]
REM ============================================================================

setlocal enabledelayedexpansion

REM 스크립트 디렉토리 경로
set "SCRIPT_DIR=%~dp0"
set "CONVERTER=%SCRIPT_DIR%scripts\convert_pptx_to_html_v2.py"

REM 도움말 표시
if "%1"=="-h" goto :show_help
if "%1"=="--help" goto :show_help
if "%1"=="/?" goto :show_help
if "%1"=="" goto :show_help_error

REM 파라미터 설정
set "INPUT_FILE=%~1"
set "OUTPUT_DIR=%~2"
set "DPI=%~3"

if "%OUTPUT_DIR%"=="" set "OUTPUT_DIR=output"
if "%DPI%"=="" set "DPI=150"

REM 헤더 출력
call :print_header

REM 입력 파일 확인
if not exist "%INPUT_FILE%" (
    echo [91m❌ 오류: 파일을 찾을 수 없습니다: %INPUT_FILE%[0m
    exit /b 1
)

REM Python 확인
where python >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo [91m❌ 오류: Python이 설치되어 있지 않습니다[0m
    echo.
    echo Python 3.7 이상을 설치해주세요:
    echo https://www.python.org/downloads/
    exit /b 1
)

REM 변환기 스크립트 확인
if not exist "%CONVERTER%" (
    echo [91m❌ 오류: 변환기를 찾을 수 없습니다: %CONVERTER%[0m
    exit /b 1
)

REM 출력 디렉토리 생성
if not exist "%OUTPUT_DIR%" mkdir "%OUTPUT_DIR%"

REM 설정 정보 출력
echo [94m📄 입력 파일:[0m %INPUT_FILE%
echo [94m📁 출력 디렉토리:[0m %OUTPUT_DIR%
echo [94m🖼️  이미지 DPI:[0m %DPI%
echo.
echo [93m🔄 변환 시작...[0m
echo.

REM 변환 실행
set "START_TIME=%TIME%"
python "%CONVERTER%" "%INPUT_FILE%" "%OUTPUT_DIR%" %DPI%
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo ================================================================

REM 결과 확인
if %EXIT_CODE% equ 0 (
    echo [92m✅ 변환 완료![0m
    echo.
    echo [94m📂 출력 파일:[0m

    if exist "%OUTPUT_DIR%\presentation.html" (
        echo   [92m✓[0m %OUTPUT_DIR%\presentation.html
    )
    if exist "%OUTPUT_DIR%\presentation_report.md" (
        echo   [92m✓[0m %OUTPUT_DIR%\presentation_report.md
    )
    if exist "%OUTPUT_DIR%\conversion.log" (
        echo   [92m✓[0m %OUTPUT_DIR%\conversion.log
    )
    if exist "%OUTPUT_DIR%\assets" (
        echo   [92m✓[0m %OUTPUT_DIR%\assets\
    )

    echo.
    echo [94m🌐 HTML 파일 열기:[0m
    echo   start %OUTPUT_DIR%\presentation.html
    echo.

    REM 자동으로 열기 제안
    set /p "OPEN_NOW=지금 브라우저에서 열까요? (y/n): "
    if /i "!OPEN_NOW!"=="y" (
        start "" "%OUTPUT_DIR%\presentation.html"
    )
) else (
    echo [91m❌ 변환 실패 (종료 코드: %EXIT_CODE%)[0m
    echo.
    echo [93m📋 로그 파일을 확인하세요:[0m
    if exist "%OUTPUT_DIR%\conversion.log" (
        echo   %OUTPUT_DIR%\conversion.log
    )
    exit /b %EXIT_CODE%
)

echo ================================================================
goto :eof

:show_help_error
echo [91m❌ 오류: 입력 파일을 지정해주세요[0m
echo.

:show_help
echo ================================================================
echo   PPTX to HTML Converter
echo ================================================================
echo.
echo 사용법:
echo   convert.bat ^<input.pptx^> [output_dir] [dpi]
echo.
echo 옵션:
echo   input.pptx    변환할 PowerPoint 파일 (필수)
echo   output_dir    출력 디렉토리 (기본값: output)
echo   dpi           이미지 품질 DPI (기본값: 150)
echo.
echo DPI 설정 가이드:
echo   72   - 빠른 변환, 작은 파일 크기
echo   96   - 표준 웹 품질
echo   150  - 권장 (기본값) - 고품질
echo   300  - 최고 품질, 큰 파일 크기
echo.
echo 예시:
echo   convert.bat presentation.pptx
echo   convert.bat presentation.pptx my_output
echo   convert.bat presentation.pptx my_output 300
echo.
exit /b 1

:print_header
echo ================================================================
echo   PPTX to HTML Converter v2.0
echo ================================================================
echo.
goto :eof
