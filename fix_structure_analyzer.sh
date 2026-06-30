#!/bin/bash
# =============================================================================
# fix_structure_analyzer.sh — Patches the None-formatting crash in
# analysis/structure_analyzer.py that breaks run_analysis() on every tick
# when nearest_resistance/nearest_support are still None early in a session.
#
# Run from ~/options-trader
# =============================================================================

cd "$(dirname "$0")" || exit 1

python3 << 'EOF'
with open('analysis/structure_analyzer.py') as f:
    content = f.read()

old = '''        logger.debug(
            f"Structure: {smap.structure_sequence} "
            f"SRlevels={len(smap.sr_levels)} "
            f"FVGs={len(smap.fvgs)} "
            f"OBs={len(smap.order_blocks)} "
            f"res={smap.nearest_resistance:.0f} "
            f"sup={smap.nearest_support:.0f}"
        )'''

new = '''        res_str = f"{smap.nearest_resistance:.0f}" if smap.nearest_resistance is not None else "N/A"
        sup_str = f"{smap.nearest_support:.0f}" if smap.nearest_support is not None else "N/A"
        logger.debug(
            f"Structure: {smap.structure_sequence} "
            f"SRlevels={len(smap.sr_levels)} "
            f"FVGs={len(smap.fvgs)} "
            f"OBs={len(smap.order_blocks)} "
            f"res={res_str} "
            f"sup={sup_str}"
        )'''

if old in content:
    content = content.replace(old, new)
    with open('analysis/structure_analyzer.py', 'w') as f:
        f.write(content)
    print("✅ PATCHED — structure_analyzer.py fixed")
else:
    print("❌ Pattern not found — exact text differs from expected.")
    print("   Paste lines 125-140 of analysis/structure_analyzer.py for manual fix.")
    import subprocess
    subprocess.run(["sed", "-n", "125,140p", "analysis/structure_analyzer.py"])
EOF

echo ""
echo "Verifying syntax..."
python3 -c "import ast; ast.parse(open('analysis/structure_analyzer.py').read()); print('✅ Syntax OK')"

echo ""
echo "Clearing pycache and restarting..."
find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null
sudo systemctl restart optionsbot
sleep 5

echo ""
echo "Recent log output:"
journalctl -u optionsbot -n 15 --no-pager
