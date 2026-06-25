# Quick Start Guide - Phase 2 PPTX to HTML Converter

## 5-Minute Setup

### 1. Install Dependencies

```bash
python3 -m venv path/to/venv
    source path/to/venv/bin/activate
    
pip install -r requirements.txt
```

Expected output:
```
Successfully installed python-pptx-0.6.23 openpyxl-3.1.2
```

### 2. Test the Converter

```bash
# Create a test output directory
mkdir -p test_output

# Convert a presentation (use your own PPTX file)
python scripts/convert_pptx_to_html_v2.py /path/to/your/presentation.pptx test_output/

# For filenames with spaces or special characters, use quotes:
python scripts/convert_pptx_to_html_v2.py "presentation (with spaces).pptx" test_output/
python scripts/convert_pptx_to_html_v2.py "(한글) 파일명.pptx" test_output/
```

### 3. View Results

Open the generated HTML file in your browser:
```bash
open test_output/presentation.html
```

## What You'll See

### Console Output
```
INFO: Initializing Enhanced PPTX Converter (DPI: 150)
INFO: Starting conversion: presentation.pptx
INFO: Found 10 slide(s)
INFO: Processing slide 1...
INFO: Processing slide 2...
...
INFO: Conversion complete: test_output/presentation.html

============================================================
📊 CONVERSION SUMMARY
============================================================
⏱️  Duration: 12.34 seconds
📄 Slides processed: 10
🔧 Total elements: 85
   ├─ 📊 Charts: 4
   ├─ 📋 Tables: 2
   ├─ 🎨 Custom shapes: 12
   ├─ 🔷 SmartArt: 1
   └─ 🖼️  Media files: 23

✅ Conversion completed successfully!
============================================================
```

### Generated Files

```
test_output/
├── presentation.html              # Open this in browser
├── presentation_report.md         # Detailed statistics
├── conversion.log                 # Full logs
└── assets/
    ├── slide1_img_rId2.png
    ├── slide2_video_rId5.mp4
    └── ...
```

## Navigation

- **Arrow Keys** (← →): Previous/Next slide
- **Space Bar**: Next slide
- **On-screen Buttons**: Click Previous/Next
- **Progress Bar**: Shows current position

## Features to Look For

### 📊 Charts
Look for bar charts, line charts, and pie charts - they should render with Chart.js!

### 🎨 Custom Shapes
Arrows, flowchart elements, and complex shapes now render as SVG.

### 🔷 SmartArt
SmartArt diagrams show text content with hierarchy.

### ✨ Animations
Elements may fade in or slide in when you navigate to a slide.

### 🎭 Effects
Shapes with shadows and reflections should display these effects.

## Customization

### Change DPI Quality

```bash
# Higher quality (larger files)
python scripts/convert_pptx_to_html_v2.py input.pptx output/ 300

# Standard quality (smaller files)
python scripts/convert_pptx_to_html_v2.py input.pptx output/ 96
```

### Enable Detailed Logging

```bash
python scripts/convert_pptx_to_html_v2.py input.pptx output/
# Check output/conversion.log for details
```

## Troubleshooting

### Shell Script Errors with Special Characters
If you see errors like `zsh: unknown file attribute` or `bash: syntax error`:

```bash
# ❌ Wrong (causes errors with special characters)
./convert.sh ./(동아출판) 파일명.pptx

# ✅ Correct (use quotes)
./convert.sh "(동아출판) 파일명.pptx"
./convert.sh "presentation (with spaces).pptx"

# ✅ Or use Python directly (no quoting issues)
python scripts/convert_pptx_to_html_v2.py "(동아출판) 파일명.pptx" output/
```

### "Module not found" Error
```bash
# Make sure you installed dependencies
pip install -r requirements.txt
```

### Charts Not Rendering
- Check internet connection (Chart.js loads from CDN)
- Open browser console (F12) for JavaScript errors
- Verify chart was extracted (check logs for "Extracted chart")

### Shapes Look Wrong
- Some complex custom shapes may approximate
- Use preset shapes (arrows, flowcharts) for best results
- Check conversion log for warnings

### Python Version Issues
```bash
# Check Python version (need 3.7+)
python --version

# Use specific Python version if needed
python3.9 scripts/convert_pptx_to_html_v2.py input.pptx output/
```

## Next Steps

1. **Read Full Documentation**: See README.md for complete features
2. **Review Architecture**: Check docs/architecture.md for technical details
3. **Browse Changelog**: See docs/changelog.md for all changes
4. **Check SKILL.md**: Complete API reference and troubleshooting

## Quick Examples

### Python API

```python
from scripts.convert_pptx_to_html_v2 import EnhancedPPTXToHTMLV2

# Basic conversion
converter = EnhancedPPTXToHTMLV2('input.pptx', 'output/')
result = converter.convert()

# With options
converter = EnhancedPPTXToHTMLV2(
    'input.pptx',
    output_dir='output/',
    dpi=150,
    log_file='output/conversion.log'
)
result = converter.convert()
```

### Batch Conversion

```bash
# Convert multiple presentations
for file in presentations/*.pptx; do
    python scripts/convert_pptx_to_html_v2.py "$file" output/
done
```

## Performance Tips

1. **Use appropriate DPI**: 150 is balanced, 72 for speed, 300 for quality
2. **Clear cache**: Delete output directory between runs
3. **Monitor logs**: Check conversion.log for warnings
4. **Test incrementally**: Convert single slides first

## Support

- **Documentation**: README.md, SKILL.md
- **Logs**: Check conversion.log for detailed errors
- **Reports**: Review presentation_report.md for statistics
- **Examples**: Test with sample presentations

---

✅ **You're ready to convert presentations!**

Start with a simple presentation and explore the Phase 2 features:
- Charts render with Chart.js
- Custom shapes use SVG
- Animations preserve with CSS
- Higher quality at 150 DPI
- Comprehensive logging
