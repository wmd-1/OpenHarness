#!/usr/bin/env python3
"""
Comprehensive Logging System for PPTX to HTML Converter
생성일: 2025-01-21
설명: 변환 프로세스의 모든 단계를 추적하고 로깅하는 시스템
"""

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List
from enum import Enum


class LogLevel(Enum):
    """로그 레벨 정의"""
    DEBUG = logging.DEBUG
    INFO = logging.INFO
    WARNING = logging.WARNING
    ERROR = logging.ERROR
    CRITICAL = logging.CRITICAL


class ConversionLogger:
    """변환 프로세스를 위한 전문 로거"""

    def __init__(self, log_file: Optional[Path] = None, console_level: LogLevel = LogLevel.INFO):
        """
        Args:
            log_file: 로그 파일 경로 (None이면 콘솔에만 출력)
            console_level: 콘솔 출력 레벨
        """
        self.logger = logging.getLogger('PPTXConverter')
        self.logger.setLevel(logging.DEBUG)

        # 기존 핸들러 제거 (중복 방지)
        self.logger.handlers.clear()

        # 콘솔 핸들러 설정
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(console_level.value)
        console_formatter = logging.Formatter(
            '%(levelname)s: %(message)s'
        )
        console_handler.setFormatter(console_formatter)
        self.logger.addHandler(console_handler)

        # 파일 핸들러 설정 (옵션)
        if log_file:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(log_file, encoding='utf-8')
            file_handler.setLevel(logging.DEBUG)
            file_formatter = logging.Formatter(
                '%(asctime)s - %(levelname)s - %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'
            )
            file_handler.setFormatter(file_formatter)
            self.logger.addHandler(file_handler)

        # 통계 수집
        self.stats = {
            'slides_processed': 0,
            'shapes_extracted': 0,
            'images_extracted': 0,
            'charts_extracted': 0,
            'tables_extracted': 0,
            'videos_extracted': 0,
            'audio_extracted': 0,
            'smartart_extracted': 0,
            'custom_shapes_extracted': 0,
            'errors': [],
            'warnings': []
        }

        self.start_time = datetime.now()

    def info(self, message: str):
        """정보 로그"""
        self.logger.info(message)

    def debug(self, message: str):
        """디버그 로그"""
        self.logger.debug(message)

    def warning(self, message: str, slide_num: Optional[int] = None, exception: Optional[Exception] = None):
        """경고 로그"""
        warning_msg = f"[Slide {slide_num}] {message}" if slide_num else message
        if exception:
            warning_msg += f" - {type(exception).__name__}: {str(exception)}"
        self.logger.warning(warning_msg)
        self.stats['warnings'].append(warning_msg)

    def error(self, message: str, exception: Optional[Exception] = None, slide_num: Optional[int] = None):
        """에러 로그"""
        error_msg = f"[Slide {slide_num}] {message}" if slide_num else message
        if exception:
            error_msg += f" - {type(exception).__name__}: {str(exception)}"

        self.logger.error(error_msg)
        self.stats['errors'].append(error_msg)

    def critical(self, message: str, exception: Optional[Exception] = None):
        """치명적 에러 로그"""
        critical_msg = message
        if exception:
            critical_msg += f" - {type(exception).__name__}: {str(exception)}"

        self.logger.critical(critical_msg)
        self.stats['errors'].append(f"CRITICAL: {critical_msg}")

    # === 통계 업데이트 메서드 ===

    def increment_slide(self):
        """슬라이드 카운터 증가"""
        self.stats['slides_processed'] += 1

    def increment_shape(self):
        """도형 카운터 증가"""
        self.stats['shapes_extracted'] += 1

    def increment_image(self):
        """이미지 카운터 증가"""
        self.stats['images_extracted'] += 1

    def increment_chart(self):
        """차트 카운터 증가"""
        self.stats['charts_extracted'] += 1

    def increment_table(self):
        """테이블 카운터 증가"""
        self.stats['tables_extracted'] += 1

    def increment_video(self):
        """비디오 카운터 증가"""
        self.stats['videos_extracted'] += 1

    def increment_audio(self):
        """오디오 카운터 증가"""
        self.stats['audio_extracted'] += 1

    def increment_smartart(self):
        """SmartArt 카운터 증가"""
        self.stats['smartart_extracted'] += 1

    def increment_custom_shape(self):
        """커스텀 도형 카운터 증가"""
        self.stats['custom_shapes_extracted'] += 1

    # === 요약 및 보고 ===

    def get_summary(self) -> Dict:
        """변환 통계 요약 반환"""
        elapsed_time = (datetime.now() - self.start_time).total_seconds()

        return {
            'duration_seconds': elapsed_time,
            'slides_processed': self.stats['slides_processed'],
            'total_elements': (
                self.stats['shapes_extracted'] +
                self.stats['images_extracted'] +
                self.stats['charts_extracted'] +
                self.stats['tables_extracted'] +
                self.stats['videos_extracted'] +
                self.stats['audio_extracted'] +
                self.stats['smartart_extracted']
            ),
            'charts': self.stats['charts_extracted'],
            'tables': self.stats['tables_extracted'],
            'custom_shapes': self.stats['custom_shapes_extracted'],
            'smartart': self.stats['smartart_extracted'],
            'media': self.stats['images_extracted'] + self.stats['videos_extracted'] + self.stats['audio_extracted'],
            'warnings_count': len(self.stats['warnings']),
            'errors_count': len(self.stats['errors']),
            'success': len(self.stats['errors']) == 0
        }

    def print_summary(self):
        """변환 요약을 콘솔에 출력"""
        summary = self.get_summary()

        print("\n" + "="*60)
        print("📊 CONVERSION SUMMARY")
        print("="*60)
        print(f"⏱️  Duration: {summary['duration_seconds']:.2f} seconds")
        print(f"📄 Slides processed: {summary['slides_processed']}")
        print(f"🔧 Total elements: {summary['total_elements']}")
        print(f"   ├─ 📊 Charts: {summary['charts']}")
        print(f"   ├─ 📋 Tables: {summary['tables']}")
        print(f"   ├─ 🎨 Custom shapes: {summary['custom_shapes']}")
        print(f"   ├─ 🔷 SmartArt: {summary['smartart']}")
        print(f"   └─ 🖼️  Media files: {summary['media']}")

        if summary['warnings_count'] > 0:
            print(f"\n⚠️  Warnings: {summary['warnings_count']}")
            for warning in self.stats['warnings'][:5]:  # 처음 5개만 표시
                print(f"   - {warning}")
            if summary['warnings_count'] > 5:
                print(f"   ... and {summary['warnings_count'] - 5} more warnings")

        if summary['errors_count'] > 0:
            print(f"\n❌ Errors: {summary['errors_count']}")
            for error in self.stats['errors'][:5]:  # 처음 5개만 표시
                print(f"   - {error}")
            if summary['errors_count'] > 5:
                print(f"   ... and {summary['errors_count'] - 5} more errors")

        if summary['success']:
            print(f"\n✅ Conversion completed successfully!")
        else:
            print(f"\n⚠️  Conversion completed with errors")

        print("="*60 + "\n")

    def save_detailed_report(self, output_path: Path):
        """상세 변환 리포트를 파일로 저장"""
        summary = self.get_summary()

        report_lines = [
            "# PPTX to HTML Conversion Report",
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "## Conversion Statistics",
            f"- Duration: {summary['duration_seconds']:.2f} seconds",
            f"- Slides processed: {summary['slides_processed']}",
            f"- Total elements extracted: {summary['total_elements']}",
            f"  - Charts: {summary['charts']}",
            f"  - Tables: {summary['tables']}",
            f"  - Custom shapes: {summary['custom_shapes']}",
            f"  - SmartArt diagrams: {summary['smartart']}",
            f"  - Media files: {summary['media']}",
            "",
            f"## Status: {'✅ SUCCESS' if summary['success'] else '⚠️ COMPLETED WITH ERRORS'}",
            ""
        ]

        if self.stats['warnings']:
            report_lines.append("## Warnings")
            for warning in self.stats['warnings']:
                report_lines.append(f"- {warning}")
            report_lines.append("")

        if self.stats['errors']:
            report_lines.append("## Errors")
            for error in self.stats['errors']:
                report_lines.append(f"- {error}")
            report_lines.append("")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text('\n'.join(report_lines), encoding='utf-8')
        self.info(f"Detailed report saved to: {output_path}")


# === 싱글톤 로거 인스턴스 ===
_global_logger: Optional[ConversionLogger] = None


def get_logger(log_file: Optional[Path] = None, console_level: LogLevel = LogLevel.INFO) -> ConversionLogger:
    """전역 로거 인스턴스 가져오기 (싱글톤 패턴)"""
    global _global_logger

    if _global_logger is None:
        _global_logger = ConversionLogger(log_file, console_level)

    return _global_logger


def reset_logger():
    """전역 로거 리셋 (테스트용)"""
    global _global_logger
    _global_logger = None
