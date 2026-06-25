#!/usr/bin/env python3
"""
SmartArt Text Extraction
생성일: 2025-01-21
설명: PowerPoint SmartArt 다이어그램에서 텍스트 컨텐츠 추출
"""

from typing import Dict, List, Optional
from pptx_path import normalize_pptx_path
from xml.etree import ElementTree as ET
import zipfile


class SmartArtParser:
    """SmartArt 다이어그램에서 텍스트를 추출하는 클래스"""

    def __init__(self, zip_ref: zipfile.ZipFile, ns: Dict[str, str], logger=None):
        """
        Args:
            zip_ref: PPTX 파일의 ZipFile 객체
            ns: XML 네임스페이스 딕셔너리
            logger: ConversionLogger 인스턴스
        """
        self.zip_ref = zip_ref
        self.ns = ns
        self.logger = logger

    def extract_smartart_text(self, graphic_frame: ET.Element, slide_rels_path: str) -> Optional[Dict]:
        """
        그래픽 프레임에서 SmartArt 텍스트 추출

        Args:
            graphic_frame: 그래픽 프레임 XML 엘리먼트
            slide_rels_path: 슬라이드 관계 파일 경로

        Returns:
            SmartArt 텍스트 데이터 딕셔너리 또는 None
        """
        try:
            # GraphicData에서 relIds 엘리먼트 찾기
            graphic_data = graphic_frame.find('.//a:graphicData', self.ns)
            if graphic_data is None:
                return None

            # SmartArt 타입 확인
            uri = graphic_data.get('uri')
            if uri != 'http://schemas.microsoft.com/office/drawing/2008/diagram':
                return None

            # SmartArt 관계 ID 찾기
            rel_ids_elem = graphic_data.find('.//{http://schemas.openxmlformats.org/officeDocument/2006/diagram}relIds')
            if rel_ids_elem is None:
                return None

            data_rel_id = rel_ids_elem.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}dm')

            if not data_rel_id:
                return None

            # SmartArt 데이터 파일 경로 해결
            data_path = self._resolve_smartart_path(slide_rels_path, data_rel_id)
            if not data_path:
                if self.logger:
                    self.logger.warning(f"Could not resolve SmartArt data path for relationship {data_rel_id}")
                return None

            # SmartArt 데이터 파싱
            smartart_data = self._parse_smartart_data(data_path)

            if smartart_data and self.logger:
                self.logger.increment_smartart()
                self.logger.debug(f"Extracted SmartArt with {len(smartart_data.get('nodes', []))} nodes")

            return smartart_data

        except Exception as e:
            if self.logger:
                self.logger.error(f"Failed to extract SmartArt", exception=e)
            return None

    def _resolve_smartart_path(self, slide_rels_path: str, rel_id: str) -> Optional[str]:
        """
        관계 ID를 사용하여 SmartArt 데이터 파일 경로 해결

        Args:
            slide_rels_path: 슬라이드 관계 파일 경로
            rel_id: 관계 ID

        Returns:
            SmartArt 파일 경로 또는 None
        """
        try:
            rels_content = self.zip_ref.read(slide_rels_path)
            rels_tree = ET.fromstring(rels_content)

            for rel in rels_tree.findall('.//rel:Relationship', self.ns):
                if rel.get('Id') == rel_id:
                    target = rel.get('Target')
                    # 상대 경로를 절대 경로로 변환
                    smartart_path = normalize_pptx_path(target)
                    return smartart_path

            return None

        except Exception as e:
            if self.logger:
                self.logger.error(f"Failed to resolve SmartArt path", exception=e)
            return None

    def _parse_smartart_data(self, data_path: str) -> Optional[Dict]:
        """
        SmartArt 데이터 XML 파싱

        Args:
            data_path: SmartArt 데이터 XML 경로

        Returns:
            SmartArt 데이터 딕셔너리
        """
        try:
            data_content = self.zip_ref.read(data_path)
            data_xml = ET.fromstring(data_content)

            # 데이터 모델 네임스페이스
            dgm_ns = {'dgm': 'http://schemas.openxmlformats.org/drawingml/2006/diagram'}

            # 모든 포인트(노드) 추출
            nodes = []
            pt_list = data_xml.find('.//dgm:ptLst', dgm_ns)

            if pt_list is not None:
                for pt in pt_list.findall('.//dgm:pt', dgm_ns):
                    node_id = pt.get('modelId', '')
                    node_type = pt.get('type', 'node')

                    # 텍스트 추출
                    text_body = pt.find('.//dgm:t', dgm_ns)
                    text_content = ""

                    if text_body is not None and text_body.text:
                        text_content = text_body.text
                    else:
                        # p:txBody에서 텍스트 추출 시도 (대체 경로)
                        for t_elem in pt.findall('.//{http://schemas.openxmlformats.org/drawingml/2006/main}t'):
                            if t_elem.text:
                                text_content += t_elem.text + " "

                    text_content = text_content.strip()

                    if text_content:
                        nodes.append({
                            'id': node_id,
                            'type': node_type,
                            'text': text_content
                        })

            if not nodes:
                if self.logger:
                    self.logger.warning(f"No text found in SmartArt: {data_path}")
                return None

            return {
                'type': 'smartart',
                'nodes': nodes,
                'layout': 'hierarchical'  # 레이아웃 정보는 제한적
            }

        except Exception as e:
            if self.logger:
                self.logger.error(f"Failed to parse SmartArt data: {data_path}", exception=e)
            return None

    def generate_smartart_html(self, smartart_data: Dict, position: Dict,
                               slide_width: float, slide_height: float,
                               absolute: bool = False, z_index: int = 1) -> str:
        """
        SmartArt 텍스트를 HTML로 변환 (계층적 텍스트 구조)

        Args:
            smartart_data: SmartArt 데이터
            position: 위치 정보
            slide_width: 슬라이드 너비
            slide_height: 슬라이드 높이
            absolute: 픽셀 기반 위치 여부
            z_index: z-index 값

        Returns:
            HTML 문자열
        """
        if absolute:
            styles = [
                f"position: absolute",
                f"left: {position.get('x', 0.0):.2f}px",
                f"top: {position.get('y', 0.0):.2f}px",
                f"width: {position.get('width', 0.0):.2f}px",
                f"height: {position.get('height', 0.0):.2f}px",
                f"z-index: {z_index}",
                "background: rgba(240, 240, 240, 0.85)",
                "border: 2px dashed #999",
                "border-radius: 8px",
                "padding: 15px",
                "overflow: auto",
                "font-family: Arial, sans-serif",
                "font-size: 14px"
            ]
        else:
            left_pct = (position['x'] / slide_width) * 100
            top_pct = (position['y'] / slide_height) * 100
            width_pct = (position['width'] / slide_width) * 100
            height_pct = (position['height'] / slide_height) * 100

            styles = [
                f"position: absolute",
                f"left: {left_pct:.2f}%",
                f"top: {top_pct:.2f}%",
                f"width: {width_pct:.2f}%",
                f"height: {height_pct:.2f}%",
                "background: rgba(240, 240, 240, 0.8)",
                "border: 2px dashed #999",
                "border-radius: 8px",
                "padding: 15px",
                "overflow: auto",
                "font-family: Arial, sans-serif",
                "font-size: 14px"
            ]

        # 노드 리스트 HTML 생성
        nodes_html = []
        for node in smartart_data.get('nodes', []):
            node_text = self._escape_html(node['text'])
            node_type = node.get('type', 'node')

            # 노드 타입에 따라 스타일 적용
            if node_type == 'doc':
                # 문서 노드 (최상위)
                nodes_html.append(f'<div style="font-weight: bold; margin-bottom: 10px; color: #333;">📄 {node_text}</div>')
            elif node_type == 'pres':
                # 프레젠테이션 노드 (중간)
                nodes_html.append(f'<div style="margin-left: 20px; margin-bottom: 8px; color: #555;">▸ {node_text}</div>')
            else:
                # 일반 노드
                nodes_html.append(f'<div style="margin-left: 40px; margin-bottom: 5px; color: #777;">• {node_text}</div>')

        # 헤더 추가
        header = '<div style="font-size: 12px; color: #888; margin-bottom: 10px; font-style: italic;">⚠️ SmartArt Diagram (text only)</div>'

        html = f'''<div style="{'; '.join(styles)}">
    {header}
    {''.join(nodes_html)}
</div>'''

        return html

    @staticmethod
    def _escape_html(text: str) -> str:
        """HTML 특수 문자 이스케이프"""
        return (text
                .replace('&', '&amp;')
                .replace('<', '&lt;')
                .replace('>', '&gt;')
                .replace('"', '&quot;')
                .replace("'", '&#39;'))
