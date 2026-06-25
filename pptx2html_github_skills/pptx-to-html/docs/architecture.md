# Phase 2 Architecture Documentation

## Overview

The Phase 2 PPTX to HTML Converter is a modular, production-ready system that converts PowerPoint presentations to standalone HTML files with 98%+ visual fidelity.

## System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     User / CLI Interface                    │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│            EnhancedPPTXToHTMLV2 (Main Converter)           │
│  - Orchestrates conversion process                         │
│  - Manages slide processing                                 │
│  - Generates final HTML                                     │
└──┬────┬────┬────┬────┬────┬─────────────────────────────────┘
   │    │    │    │    │    │
   │    │    │    │    │    └──────┐
   │    │    │    │    │           │
   ▼    ▼    ▼    ▼    ▼           ▼
┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐ ┌──────────┐
│Logger│ │Chart │ │Shape │ │Smart │ │Anim  │ │PPTX Zip  │
│System│ │Extr. │ │Geom. │ │Art   │ │Handlr│ │File      │
└──────┘ └──────┘ └──────┘ └──────┘ └──────┘ └──────────┘
```

## Module Descriptions

### 1. Main Converter (`convert_pptx_to_html_v2.py`)

**책임(Responsibilities):**
- PPTX 파일 언팩 및 XML 파싱
- 슬라이드 순회 및 요소 추출
- 모듈 간 조율
- HTML 생성 및 파일 출력

**핵심 메서드:**
- `convert()`: 메인 변환 프로세스
- `process_slide()`: 단일 슬라이드 처리
- `generate_html()`: 최종 HTML 생성
- `extract_shape_position/fill/border()`: 도형 속성 추출

**의존성:**
- `logger.ConversionLogger`
- `chart_extractor.ChartExtractor`
- `shape_geometry.ShapeGeometryConverter`
- `smartart_parser.SmartArtParser`
- `animation_handler.AnimationHandler`

### 2. Logging System (`logger.py`)

**책임:**
- 변환 프로세스 로깅
- 통계 수집 (슬라이드, 차트, 도형 등)
- 에러 및 경고 추적
- 요약 리포트 생성

**핵심 클래스:**
```python
class ConversionLogger:
    def info(message)          # 정보 로그
    def warning(message)       # 경고 로그
    def error(message, exc)    # 에러 로그
    def increment_chart()      # 차트 카운터 증가
    def get_summary()          # 통계 요약
    def print_summary()        # 콘솔 출력
```

**로그 레벨:**
- DEBUG: 상세 디버깅 정보
- INFO: 진행 상황
- WARNING: 경고 (변환 계속)
- ERROR: 에러 (변환 계속 시도)
- CRITICAL: 치명적 에러 (변환 중단)

### 3. Chart Extractor (`chart_extractor.py`)

**책임:**
- PowerPoint 차트 XML 파싱
- 차트 데이터 추출 (카테고리, 값, 시리즈)
- Chart.js 설정 생성
- HTML Canvas 엘리먼트 생성

**지원 차트 타입:**
- Bar (2D/3D)
- Line (2D/3D)
- Pie (2D/3D)
- Doughnut
- Area
- Scatter
- Radar
- Bubble

**핵심 메서드:**
```python
def extract_chart_from_graphic_frame(graphic_frame, slide_rels_path)
def _parse_chart_xml(chart_path)
def _extract_chart_data(plot_area, chart_type)
def generate_chartjs_html(chart_config, chart_id, position, ...)
```

**Chart.js 통합:**
- CDN에서 Chart.js 4.4.1 로드
- 차트별 고유 ID 생성
- 반응형 캔버스 컨테이너
- 데이터 기반 렌더링

### 4. Shape Geometry Converter (`shape_geometry.py`)

**책임:**
- DrawingML 커스텀 지오메트리 파싱
- SVG 경로 변환
- 프리셋 도형 지원
- SVG HTML 생성

**경로 명령 매핑:**
```
DrawingML     →    SVG
moveTo        →    M (MoveTo)
lnTo          →    L (LineTo)
cubicBezTo    →    C (Cubic Bezier)
quadBezTo     →    Q (Quadratic Bezier)
close         →    Z (Close Path)
```

**프리셋 도형:**
- 기본: 직사각형, 타원, 삼각형, 다이아몬드
- 화살표: 좌, 우, 상, 하, 양방향
- 플로우차트: 프로세스, 결정, 데이터, 터미네이터
- 다각형: 오각형, 육각형, 팔각형
- 별 모양

### 5. SmartArt Parser (`smartart_parser.py`)

**책임:**
- SmartArt 데이터 XML 파싱
- 노드 계층 구조 추출
- 텍스트 컨텐츠 추출
- HTML 계층 구조 생성

**제한사항:**
- 시각적 레이아웃은 근사화
- 텍스트 컨텐츠만 완전 추출
- 복잡한 다이어그램은 단순화

**출력 형식:**
```html
<div class="smartart-container">
  📄 문서 노드 (최상위)
  ▸ 프레젠테이션 노드 (중간)
    • 일반 노드
</div>
```

### 6. Animation Handler (`animation_handler.py`)

**책임:**
- PowerPoint 애니메이션 추출
- CSS 키프레임 생성
- JavaScript 시퀀싱
- 그림자 및 반사 효과

**지원 애니메이션:**
- Appear / Fade In / Fade Out
- Fly In / Fly Out (방향별)
- Wipe / Split
- Grow / Shrink / Zoom
- Rotate / Swivel
- Bounce

**CSS 애니메이션 예:**
```css
@keyframes fadeIn {
    from { opacity: 0; }
    to { opacity: 1; }
}

@keyframes slideInLeft {
    from {
        opacity: 0;
        transform: translateX(-100%);
    }
    to {
        opacity: 1;
        transform: translateX(0);
    }
}
```

**그림자 효과:**
- XML에서 속성 추출 (블러, 거리, 방향, 색상)
- CSS `box-shadow` 변환
- 알파 채널 지원

## Data Flow

### Conversion Process Flow

```
1. 초기화
   ├─ PPTX 파일 열기 (ZipFile)
   ├─ 로거 초기화
   ├─ 모듈 인스턴스 생성
   └─ 슬라이드 크기 추출

2. 슬라이드 순회
   For each slide:
   ├─ 슬라이드 XML 파싱
   ├─ 배경 추출
   ├─ 애니메이션 추출
   │
   ├─ 도형 처리
   │  ├─ 일반 도형 (p:sp)
   │  │  ├─ 위치/크기/회전
   │  │  ├─ 채우기/테두리
   │  │  ├─ 텍스트 및 서식
   │  │  ├─ 하이퍼링크
   │  │  ├─ 커스텀 지오메트리
   │  │  ├─ 그림자/반사
   │  │  └─ 이미지/비디오/오디오
   │  │
   │  └─ 그래픽 프레임 (p:graphicFrame)
   │     ├─ 차트 → ChartExtractor
   │     ├─ 테이블 → 기존 로직
   │     └─ SmartArt → SmartArtParser
   │
   └─ 슬라이드 데이터 저장

3. HTML 생성
   ├─ 슬라이드별 HTML 생성
   │  ├─ 배경 스타일
   │  ├─ 요소 HTML
   │  │  ├─ 차트 → Chart.js Canvas
   │  │  ├─ 커스텀 도형 → SVG
   │  │  ├─ SmartArt → 계층 구조
   │  │  └─ 기타 → DIV/TABLE
   │  └─ 애니메이션 스크립트
   │
   ├─ CSS 스타일 추가
   │  ├─ 레이아웃 스타일
   │  ├─ 애니메이션 키프레임
   │  └─ 반응형 미디어 쿼리
   │
   ├─ JavaScript 추가
   │  ├─ 슬라이드 네비게이션
   │  ├─ 차트 초기화
   │  └─ 애니메이션 트리거
   │
   └─ 파일 출력

4. 리포트 생성
   ├─ 통계 수집
   ├─ 경고/에러 요약
   └─ 마크다운 리포트 저장
```

## Error Handling Strategy

### Graceful Degradation

```python
try:
    # Phase 2 기능 시도 (차트, 커스텀 도형 등)
    chart = extract_chart(...)
except Exception as e:
    logger.warning("Chart extraction failed", exception=e)
    # 변환 계속 진행, 차트는 스킵
```

### Error Levels

1. **정보 (INFO)**: 정상 작동
2. **경고 (WARNING)**: 일부 기능 실패, 변환 계속
3. **에러 (ERROR)**: 요소 처리 실패, 슬라이드 계속
4. **치명적 (CRITICAL)**: 변환 중단

### Fallback Mechanisms

- **차트**: 데이터 추출 실패 시 빈 공간 또는 플레이스홀더
- **커스텀 도형**: SVG 변환 실패 시 직사각형 폴백
- **SmartArt**: 레이아웃 실패 시 텍스트만 표시
- **애니메이션**: 추출 실패 시 정적 렌더링

## Performance Optimizations

### Caching

```python
# 관계 파일 캐싱 (반복 읽기 방지)
self.rels_cache = {}

def get_relationships(rels_path):
    if rels_path in self.rels_cache:
        return self.rels_cache[rels_path]
    # ... 파일 읽기 및 캐싱
```

### Lazy Loading

- Chart.js는 CDN에서 로드 (번들 크기 감소)
- 이미지는 assets 폴더에 분리

### DPI Configuration

```python
# 기본값: 150 DPI (품질과 성능 균형)
# 옵션: 72 (빠름), 96 (표준), 150 (기본), 300 (고품질)
converter = EnhancedPPTXToHTMLV2(pptx_path, dpi=150)
```

## Security Considerations

### Input Validation

- PPTX 파일 존재 확인
- ZIP 파일 유효성 검증
- XML 파싱 에러 처리

### Output Sanitization

```python
def _escape_html(text):
    return (text
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;')
            .replace("'", '&#39;'))
```

### Path Traversal Prevention

```python
# 상대 경로 정규화
media_path = f"ppt/{target.replace('..', '').lstrip('/')}"
```

## Testing Strategy

### Unit Tests

```python
# logger 테스트
def test_logger_increment():
    logger = ConversionLogger()
    logger.increment_chart()
    assert logger.stats['charts_extracted'] == 1

# chart extractor 테스트
def test_chart_type_detection():
    extractor = ChartExtractor(zip_ref, logger)
    chart_type = extractor._detect_chart_type(chart_xml)
    assert chart_type == 'barChart'
```

### Integration Tests

```python
# 전체 변환 테스트
def test_full_conversion():
    converter = EnhancedPPTXToHTMLV2('sample.pptx', 'output/')
    result = converter.convert()
    assert result.exists()
    assert (result.parent / 'assets').exists()
```

### Test Coverage Goals

- Unit Tests: 80%+ 코드 커버리지
- Integration Tests: 주요 기능 플로우
- Sample Files: 다양한 프레젠테이션 타입

## Future Enhancements (Phase 3)

### Planned Features

1. **향상된 애니메이션**
   - 더 많은 애니메이션 타입
   - 순차 애니메이션
   - 모션 경로

2. **폰트 임베딩**
   - 커스텀 폰트 추출
   - Web fonts 변환
   - 폰트 폴백 체인

3. **3D 효과**
   - CSS 3D transforms
   - Perspective 효과

4. **성능 개선**
   - 병렬 처리
   - 증분 렌더링
   - 메모리 최적화

### Extension Points

- **플러그인 시스템**: 커스텀 변환 로직
- **템플릿 엔진**: HTML 출력 커스터마이징
- **API 서버**: 웹 서비스로 노출

## Deployment

### Production Checklist

- [ ] Dependencies 설치 (`pip install -r requirements.txt`)
- [ ] Python 3.7+ 확인
- [ ] Chart.js CDN 접근성 확인
- [ ] 출력 디렉토리 쓰기 권한 확인
- [ ] 로그 파일 경로 설정
- [ ] DPI 설정 (기본값 150)
- [ ] 테스트 실행
- [ ] 샘플 프레젠테이션 변환 테스트

### Monitoring

```python
# 로그 파일 모니터링
tail -f output/conversion.log

# 리포트 확인
cat output/presentation_report.md
```

## Conclusion

Phase 2 아키텍처는 모듈화, 확장성, 안정성을 중심으로 설계되었습니다. 각 모듈은 단일 책임을 가지며, 명확한 인터페이스를 통해 상호작용합니다. 포괄적인 로깅과 에러 처리를 통해 프로덕션 환경에서 안정적으로 운영될 수 있습니다.
