# High-Fidelity PPTX → HTML Conversion Plan

## Goals
- 완전한 레이아웃 일치(픽셀 단위)와 요소 z-순서 보장
- 프레젠테이션 폰트 내장(woff/woff2)으로 브라우저 렌더링 일관성 확보
- 텍스트, 도형, 차트, SmartArt 등을 가능하면 PPTX 구조 기반으로 의미 있게 재구성
- 모든 주요 브라우저 지원(Chrome 우선), 오프라인 가능 단일 번들 출력 허용

## Conversion Pipeline
1. **PPTX Loader**
   - Open XML 부품 파싱, 슬라이드 마스터/레이아웃 상속 완전 적용
   - Relationship/Theme/Font mapping 캐시
2. **Asset Extractor**
   - 이미지/비디오/오디오 원본 추출
   - Font embedding: `ppt/fonts/*.odttf` → ttf → woff/woff2 변환
3. **Layout Engine**
   - 슬라이드 좌표계를 픽셀 단위 그대로 사용 (EMU → px @ 96dpi 기준)
   - 뷰포트 대응: 고정 크기 stage + CSS transform scale (responsive wrapper)
   - Z-order: stacking context from XML order & `z-index`
4. **Element Builders**
   - Text frames → `<div><p><span>` 구조, line spacing/indent/탭 정확히 반영
   - Shapes → `<svg>` per shape with native EMU coords, gradient/패턴/그림자 CSS/SVG filter
   - Groups → `<div>` with nested transforms preserving relative positions
   - Charts → Semantic recreation via Chart.js; fallback bitmap when unsupported
   - SmartArt → Hierarchical reconstruction; fallback to SVG snapshot
   - Tables → `<table>` with precise cell metrics, padding, border styles
5. **Animation Timeline**
   - PPT animation tree → JSON timeline → runtime JS animator (GSAP or custom)
6. **HTML Bundle Writer**
   - Emits `index.html`, `presentation.css`, `presentation.js`, `assets/`, `fonts/`
   - Preload manifest + lazy loading for large media

## Key Technical Decisions
- **Coordinate Preservation:** 모든 위치/크기는 픽셀 값으로 CSS absolute positioning (`left/top/width/height`), wrapper scale 로 반응형 구현.
- **Font Handling:** `ppt/fonts` 및 테마 글꼴 매핑; woff/woff2 출력 후 `@font-face` 등록; 텍스트 span에 실제 폰트명 지정.
- **Paragraph Fidelity:** line spacing (`spcBef`, `spcAft`, `lnSpc`), indentation (`marL`, `marR`, `indent`), bullet 형식 (`buAutoNum`, `buChar`) 전부 CSS로 변환; 리스트 계층을 `<ul>/<ol>` 대신 span+CSS로 재현해 레이아웃 유지.
- **Effects:** Shadows/reflections → CSS filters; blur/glow → filter + pseudo elements; gradient stops → SVG gradient definitions respecting angle/scale.
- **Z-Index Strategy:** PPT stack order → incremental `z-index`; groups maintain relative z via nested containers.
- **Fallback Rasterization:** WordArt, 복잡한 SmartArt, 3D shapes 등 복원 어려운 요소는 Pillow/SVG rasterizer로 PNG 생성 (DPI 150+), 메타데이터에 fallback 표시.
- **Testing:** 샘플 PPTX 모음으로 픽셀 diff 스냅샷 비교 (Pillow) + DOM structure asserts.

## Work Breakdown
1. Core refactor of converter orchestrator (slide/master resolution, absolute canvas)
2. Font extraction + conversion utility
3. Layout & text renderer rewrite (paragraph metrics)
4. Shape/SVG engine upgrades (gradient, patterns, grouping)
5. Chart & SmartArt fidelity enhancements/fallbacks
6. Animation timeline exporter + JS runtime
7. HTML/CSS/JS template restructure (external bundles, asset manifest)
8. QA harness & regression tests

## Risks & Mitigation
- **Font conversion complexity:** Use `fontTools` to convert; fallback warn 시 로깅
- **Performance for large decks:** Lazy asset extraction, streaming writes
- **Spec coverage:** incremental module tests per OOXML section, log unsupported features clearly

## Next Steps
1. Implement slide/master inheritance + absolute canvas refactor
2. Integrate font embedding pipeline
3. Rebuild text layout engine with precise metrics
4. Extend shape/gradient handling with SVG defs
