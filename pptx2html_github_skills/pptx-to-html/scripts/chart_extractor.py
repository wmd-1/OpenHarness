#!/usr/bin/env python3
"""
Chart Extraction and Chart.js Integration
생성일: 2025-01-21
설명: PowerPoint 차트를 추출하여 Chart.js 형식으로 변환
"""

from typing import Dict, List, Optional, Tuple
from pptx_path import normalize_pptx_path
from xml.etree import ElementTree as ET
import zipfile
from pathlib import Path
import io


class ChartExtractor:
    """PowerPoint 차트를 Chart.js 형식으로 변환하는 클래스"""

    def __init__(self, zip_ref: zipfile.ZipFile, logger=None):
        """
        Args:
            zip_ref: PPTX 파일의 ZipFile 객체
            logger: ConversionLogger 인스턴스
        """
        self.zip_ref = zip_ref
        self.logger = logger

        # XML 네임스페이스
        self.ns = {
            'c': 'http://schemas.openxmlformats.org/drawingml/2006/chart',
            'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
            'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
            'p': 'http://schemas.openxmlformats.org/presentationml/2006/main',
            'rel': 'http://schemas.openxmlformats.org/package/2006/relationships'
        }

        # 차트 타입 매핑 (PowerPoint → Chart.js)
        self.chart_type_mapping = {
            'barChart': 'bar',
            'bar3DChart': 'bar',
            'lineChart': 'line',
            'line3DChart': 'line',
            'pieChart': 'pie',
            'pie3DChart': 'pie',
            'doughnutChart': 'doughnut',
            'areaChart': 'line',  # Chart.js uses line with fill
            'area3DChart': 'line',
            'scatterChart': 'scatter',
            'radarChart': 'radar',
            'bubbleChart': 'bubble'
        }

    def extract_chart_from_graphic_frame(self, graphic_frame: ET.Element, slide_rels_path: str) -> Optional[Dict]:
        """
        그래픽 프레임에서 차트 데이터를 추출

        Args:
            graphic_frame: 그래픽 프레임 XML 엘리먼트
            slide_rels_path: 슬라이드 관계 파일 경로

        Returns:
            차트 설정 딕셔너리 또는 None
        """
        try:
            # 차트 관계 ID 찾기
            chart_rel = graphic_frame.find('.//c:chart', self.ns)
            if chart_rel is None:
                # c:chart가 없으면 a:graphic에서 찾기
                graphic_data = graphic_frame.find('.//a:graphic//a:graphicData', self.ns)
                if graphic_data is not None:
                    chart_rel = graphic_data.find('.//c:chart', self.ns)

            if chart_rel is None:
                return None

            rel_id = chart_rel.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')
            if not rel_id:
                return None

            # 차트 파일 경로 해결
            chart_path = self._resolve_chart_path(slide_rels_path, rel_id)
            if not chart_path:
                if self.logger:
                    self.logger.warning(f"Could not resolve chart path for relationship {rel_id}")
                return None

            # 차트 XML 파싱
            chart_data = self._parse_chart_xml(chart_path)

            if chart_data and self.logger:
                self.logger.increment_chart()
                self.logger.debug(f"Extracted {chart_data.get('type', 'unknown')} chart")

            return chart_data

        except Exception as e:
            if self.logger:
                self.logger.error(f"Failed to extract chart from graphic frame", exception=e)
            return None

    def _resolve_chart_path(self, slide_rels_path: str, rel_id: str) -> Optional[str]:
        """
        관계 ID를 사용하여 차트 파일 경로 해결

        Args:
            slide_rels_path: 슬라이드 관계 파일 경로
            rel_id: 관계 ID

        Returns:
            차트 파일 경로 또는 None
        """
        try:
            rels_content = self.zip_ref.read(slide_rels_path)
            rels_tree = ET.fromstring(rels_content)

            for rel in rels_tree.findall('.//rel:Relationship', self.ns):
                if rel.get('Id') == rel_id:
                    target = rel.get('Target')
                    # 상대 경로를 절대 경로로 변환
                    chart_path = normalize_pptx_path(target)
                    return chart_path

            return None

        except Exception as e:
            if self.logger:
                self.logger.error(f"Failed to resolve chart path", exception=e)
            return None

    def _parse_chart_xml(self, chart_path: str) -> Optional[Dict]:
        """
        차트 XML 파일을 파싱하여 Chart.js 형식으로 변환

        Args:
            chart_path: 차트 XML 파일 경로

        Returns:
            Chart.js 설정 딕셔너리
        """
        try:
            chart_content = self.zip_ref.read(chart_path)
            chart_xml = ET.fromstring(chart_content)

            # 차트 타입 결정
            chart_type = self._detect_chart_type(chart_xml)
            if not chart_type:
                if self.logger:
                    self.logger.warning(f"Unknown chart type in {chart_path}")
                return None

            # 차트 데이터 추출
            chart_space = chart_xml.find('.//c:chartSpace', self.ns)
            if chart_space is None:
                return None

            chart_element = chart_space.find('.//c:chart', self.ns)
            if chart_element is None:
                return None

            # 제목 추출
            title = self._extract_chart_title(chart_element)

            # 데이터 추출
            plot_area = chart_element.find('.//c:plotArea', self.ns)
            if plot_area is None:
                return None

            # 차트 타입별 데이터 추출
            labels, datasets = self._extract_chart_data(plot_area, chart_type)

            if not labels or not datasets:
                if self.logger:
                    self.logger.warning(f"No data found in chart {chart_path}")
                return None

            # Chart.js 형식으로 변환
            chartjs_config = {
                'type': self.chart_type_mapping.get(chart_type, 'bar'),
                'title': title,
                'data': {
                    'labels': labels,
                    'datasets': datasets
                },
                'options': self._generate_chart_options(chart_type, title)
            }

            return chartjs_config

        except Exception as e:
            if self.logger:
                self.logger.error(f"Failed to parse chart XML: {chart_path}", exception=e)
            return None

    def _detect_chart_type(self, chart_xml: ET.Element) -> Optional[str]:
        """
        차트 타입 감지

        Args:
            chart_xml: 차트 XML 루트 엘리먼트

        Returns:
            차트 타입 문자열
        """
        # 지원하는 차트 타입 검색
        for chart_type in self.chart_type_mapping.keys():
            if chart_xml.find(f'.//{{{self.ns["c"]}}}{chart_type}') is not None:
                return chart_type

        return None

    def _extract_chart_title(self, chart_element: ET.Element) -> str:
        """
        차트 제목 추출

        Args:
            chart_element: 차트 엘리먼트

        Returns:
            차트 제목 문자열
        """
        title_elem = chart_element.find('.//c:title//a:t', self.ns)
        if title_elem is not None and title_elem.text:
            return title_elem.text

        return "Chart"

    def _extract_chart_data(self, plot_area: ET.Element, chart_type: str) -> Tuple[List[str], List[Dict]]:
        """
        차트 데이터 추출

        Args:
            plot_area: 플롯 영역 엘리먼트
            chart_type: 차트 타입

        Returns:
            (레이블 리스트, 데이터셋 리스트) 튜플
        """
        labels = []
        datasets = []

        # 차트 타입 엘리먼트 찾기
        chart_elem = plot_area.find(f'.//c:{chart_type}', self.ns)
        if chart_elem is None:
            return labels, datasets

        # 시리즈 추출
        series_list = chart_elem.findall('.//c:ser', self.ns)

        for ser in series_list:
            # 시리즈 이름
            series_name = self._extract_series_name(ser)

            # 카테고리 (X축 레이블)
            if not labels:
                labels = self._extract_categories(ser)

            # 값 (Y축 데이터)
            values = self._extract_values(ser)

            # 시리즈 색상
            color = self._extract_series_color(ser)

            # 데이터셋 구성
            dataset = {
                'label': series_name,
                'data': values,
                'backgroundColor': color if chart_type in ['pieChart', 'pie3DChart', 'doughnutChart'] else self._adjust_alpha(color, 0.6),
                'borderColor': color,
                'borderWidth': 2
            }

            # 영역 차트의 경우 fill 속성 추가
            if 'area' in chart_type.lower():
                dataset['fill'] = True

            datasets.append(dataset)

        return labels, datasets

    def _extract_series_name(self, series_elem: ET.Element) -> str:
        """시리즈 이름 추출"""
        tx = series_elem.find('.//c:tx', self.ns)
        if tx is not None:
            # v 엘리먼트에서 값 찾기
            v = tx.find('.//c:v', self.ns)
            if v is not None and v.text:
                return v.text

            # strRef에서 찾기
            str_ref = tx.find('.//c:strRef//c:v', self.ns)
            if str_ref is not None and str_ref.text:
                return str_ref.text

        return "Series"

    def _extract_categories(self, series_elem: ET.Element) -> List[str]:
        """카테고리 (X축 레이블) 추출"""
        categories = []

        cat = series_elem.find('.//c:cat', self.ns)
        if cat is not None:
            # strRef에서 포인트 찾기
            pts = cat.findall('.//c:pt', self.ns)
            for pt in pts:
                v = pt.find('.//c:v', self.ns)
                if v is not None and v.text:
                    categories.append(v.text)

        return categories

    def _extract_values(self, series_elem: ET.Element) -> List[float]:
        """값 (Y축 데이터) 추출"""
        values = []

        val = series_elem.find('.//c:val', self.ns)
        if val is not None:
            # numRef에서 포인트 찾기
            pts = val.findall('.//c:pt', self.ns)
            for pt in pts:
                v = pt.find('.//c:v', self.ns)
                if v is not None and v.text:
                    try:
                        values.append(float(v.text))
                    except ValueError:
                        values.append(0)

        return values

    def _extract_series_color(self, series_elem: ET.Element) -> str:
        """시리즈 색상 추출"""
        # 기본 색상 팔레트 (Chart.js 기본값)
        default_colors = [
            'rgb(54, 162, 235)',   # 파란색
            'rgb(255, 99, 132)',   # 빨간색
            'rgb(255, 205, 86)',   # 노란색
            'rgb(75, 192, 192)',   # 청록색
            'rgb(153, 102, 255)',  # 보라색
            'rgb(255, 159, 64)'    # 주황색
        ]

        # XML에서 색상 추출 시도
        solid_fill = series_elem.find('.//c:spPr//a:solidFill', self.ns)
        if solid_fill is not None:
            rgb = solid_fill.find('.//a:srgbClr', self.ns)
            if rgb is not None:
                color_val = rgb.get('val')
                if color_val:
                    return f'#{color_val}'

        # 기본 색상 반환 (시리즈 인덱스 기반)
        ser_idx = 0  # 실제로는 시리즈 순서를 추적해야 함
        return default_colors[ser_idx % len(default_colors)]

    def _adjust_alpha(self, color: str, alpha: float) -> str:
        """
        색상에 알파 값 적용

        Args:
            color: RGB 색상 문자열
            alpha: 알파 값 (0.0 ~ 1.0)

        Returns:
            RGBA 색상 문자열
        """
        if color.startswith('rgb('):
            return color.replace('rgb(', f'rgba(').replace(')', f', {alpha})')
        elif color.startswith('#'):
            # Hex to RGBA
            hex_color = color.lstrip('#')
            r = int(hex_color[0:2], 16)
            g = int(hex_color[2:4], 16)
            b = int(hex_color[4:6], 16)
            return f'rgba({r}, {g}, {b}, {alpha})'

        return color

    def _generate_chart_options(self, chart_type: str, title: str) -> Dict:
        """
        Chart.js 옵션 생성

        Args:
            chart_type: 차트 타입
            title: 차트 제목

        Returns:
            Chart.js options 딕셔너리
        """
        options = {
            'responsive': True,
            'maintainAspectRatio': True,
            'plugins': {
                'legend': {
                    'display': True,
                    'position': 'top'
                },
                'title': {
                    'display': bool(title and title != "Chart"),
                    'text': title
                }
            }
        }

        # 차트 타입별 추가 옵션
        if chart_type in ['barChart', 'bar3DChart']:
            options['scales'] = {
                'y': {
                    'beginAtZero': True
                }
            }

        return options

    def generate_chartjs_html(self, chart_config: Dict, chart_id: str, position: Dict, slide_width: float, slide_height: float) -> str:
        """
        Chart.js HTML/Canvas 엘리먼트 생성

        Args:
            chart_config: Chart.js 설정
            chart_id: 차트 고유 ID
            position: 위치 정보 딕셔너리 (x, y, width, height)
            slide_width: 슬라이드 너비 (px)
            slide_height: 슬라이드 높이 (px)

        Returns:
            HTML 문자열
        """
        # 퍼센트 기반 위치 계산
        left_pct = (position['x'] / slide_width) * 100
        top_pct = (position['y'] / slide_height) * 100
        width_pct = (position['width'] / slide_width) * 100
        height_pct = (position['height'] / slide_height) * 100

        # Chart.js 설정을 JSON 문자열로 변환
        import json
        config_json = json.dumps(chart_config, ensure_ascii=False)

        html = f'''<div class="chart-container" style="position: absolute; left: {left_pct:.2f}%; top: {top_pct:.2f}%; width: {width_pct:.2f}%; height: {height_pct:.2f}%;">
    <canvas id="{chart_id}"></canvas>
</div>
<script>
(function() {{
    const ctx = document.getElementById('{chart_id}').getContext('2d');
    const config = {config_json};
    new Chart(ctx, config);
}})();
</script>'''

        return html
