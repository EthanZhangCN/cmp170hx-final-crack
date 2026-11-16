#!/usr/bin/env python3
"""
apply-ndiv.py - apply the HBM NDIV overclock hooks to a patched source tree.

Marker-based instead of patch(1) context matching: every edit is anchored
to a line that must occur EXACTLY ONCE in the target file, so the edits
are immune to line-number drift and to patch(1) fuzz/search differences
between patch versions (observed: identical context + identical file,
GNU patch 2.8 on the target host refused pure-insertion hunks that
patch 2.7 accepted with fuzz).

The register #defines are read from patches/hbm-ndiv.patch (the manifest
gate in tools/read-constants.py checks those addresses there), so the
patch file remains the single source of truth for the register map.

Usage:    apply-ndiv.py <source-tree-root>
Idempotent: if the edits are already present, exits 0 without changing files.
"""
import pathlib
import sys


def die(msg):
    print("apply-ndiv: FAIL: %s" % msg, file=sys.stderr)
    sys.exit(1)


def main():
    if len(sys.argv) != 2:
        die("usage: apply-ndiv.py <source-tree-root>")
    tree = pathlib.Path(sys.argv[1])
    here = pathlib.Path(__file__).resolve().parent

    gsp = tree / 'src/nvidia/src/kernel/gpu/gsp/kernel_gsp.c'
    tu = tree / 'src/nvidia/src/kernel/gpu/gsp/arch/turing/kernel_gsp_tu102.c'
    for f in (gsp, tu):
        if not f.is_file():
            die("missing %s" % f)

    # ---- register map: single source of truth is the patch file ----
    patch_file = here / 'patches/hbm-ndiv.patch'
    if not patch_file.is_file():
        die("missing %s" % patch_file)
    defines = [l[1:] for l in patch_file.read_text().split('\n')
               if l.startswith('+') and '#define CMP_REG_' in l]
    if len(defines) != 10:
        die("expected 10 CMP_REG_* defines in %s, found %d"
            % (patch_file.name, len(defines)))

    def edit(lines, marker, block, label, mode='after'):
        hits = [i for i, l in enumerate(lines) if marker in l]
        if len(hits) != 1:
            die("%s: anchor %r found %d times (need exactly 1)"
                % (label, marker, len(hits)))
        i = hits[0]
        if mode == 'after':
            lines[i + 1:i + 1] = block
        else:
            lines[i:i] = block
        return lines

    # ================= kernel_gsp.c =================
    gsp_text = gsp.read_text()
    if 'cmp_hbm_ndiv_cfg.h' not in gsp_text:
        g = gsp_text.split('\n')

        # 1) cfg include + register map + impl include (file scope, after the
        #    crashcat include; the sec2 target-check function is defined above)
        block1 = [
            '#include "kernel/gpu/cmp_hbm_ndiv_cfg.h"',
            '#ifdef CMPUNLOCK_MCLK_NDIV',
            '',
            '/* HBM NDIV register map (manifest: common/constants.yaml hbm_ndiv) */',
        ] + defines + [
            '',
            '#endif',
            '#include "kernel/gpu/cmp_hbm_ndiv_impl.h"',
        ]
        g = edit(g, '#include "crashcat/crashcat_report.h"',
                 block1, 'kernel_gsp.c includes')

        # 2) PLM gates so the 3 FBPA PLL registers get opened, after the last
        #    existing table entry
        block2 = [
            '#ifdef CMPUNLOCK_MCLK_NDIV',
            '            { 0x009a3c7cU, 0xffffffffU, "FBPA_PLL0" },',
            '            { 0x009a3c80U, 0xffffffffU, "FBPA_PLL1" },',
            '            { 0x009a3c84U, 0xffffffffU, "FBPA_PLL2" },',
            '#endif',
        ]
        g = edit(g, '"PJTAG_SEC_PLM"', block2, 'kernel_gsp.c PLM gates')

        # 3) loop over the whole (now longer) table instead of a fixed 12
        hits = [i for i, l in enumerate(g) if 'plmIdx < 12' in l]
        if len(hits) != 1:
            die("kernel_gsp.c loop: 'plmIdx < 12' found %d times (need 1)"
                % len(hits))
        g[hits[0]] = g[hits[0]].replace(
            'plmIdx < 12', 'plmIdx < NV_ARRAY_ELEMENTS(plmTable)')

        # 4) second NDIV half, after the GSP log-polling start (before done:)
        block4 = [
            '#ifdef CMPUNLOCK_MCLK_NDIV',
            '    kgspCmpHbmNdivPostGsp(pGpu, pKernelGsp);',
            '#endif',
        ]
        g = edit(g, 'kgspStartLogPolling(pGpu, pKernelGsp), done',
                 block4, 'kernel_gsp.c post-GSP call')

        gsp.write_text('\n'.join(g))
    else:
        print("apply-ndiv: kernel_gsp.c already patched - skipping")

    # ================= kernel_gsp_tu102.c =================
    tu_text = tu.read_text()
    if 'cmp_hbm_ndiv_cfg.h' not in tu_text:
        t = tu_text.split('\n')

        # 5) cfg include at file scope (after the last SEC2 define block)
        t = edit(t, '#define SEC2_DEBUG_PRI_MMU_LMR',
                 ['#include "kernel/gpu/cmp_hbm_ndiv_cfg.h"', ''],
                 'tu102 include')

        # 6) first NDIV half: after Booter Load, before the init RPCs
        block6 = [
            '    // HBM NDIV overclock: first half, after Booter Load, before GSP runs.',
            '#ifdef CMPUNLOCK_MCLK_NDIV',
            '    kgspCmpHbmNdivPostBooterLoad(pGpu, pKernelGsp);',
            '#endif',
            '',
        ]
        t = edit(t, '// Send init RPCs necessary for GSP-RM booting',
                 block6, 'tu102 BooterLoad call', mode='before')

        tu.write_text('\n'.join(t))
    else:
        print("apply-ndiv: kernel_gsp_tu102.c already patched - skipping")

    print("apply-ndiv: OK")


if __name__ == '__main__':
    main()
