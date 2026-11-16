/*
 * cmp_hbm_ndiv_impl.h - HBM PLL (NDIV) overclock implementation
 *
 * Copied verbatim by driver/build.sh into the NVIDIA source tree
 * (src/nvidia/inc/kernel/gpu/) and included from kernel_gsp.c.
 * Kept as a real file (instead of a patch hunk) so the C code itself
 * never has to match source-tree context.
 *
 * Ported from asm64-hooligan's cmpunlocker fork (driver/src/cmpunlock.c).
 * Requires CMPUNLOCK_MCLK_NDIV to be defined (done by the generated
 * cmp_hbm_ndiv_cfg.h, which is included before this file) and the
 * sec2 unlock's _kgspSec2PostblTimingEnabled() (defined above the
 * include point in kernel_gsp.c).
 */

#ifndef CMP_HBM_NDIV_IMPL_H
#define CMP_HBM_NDIV_IMPL_H

#ifdef CMPUNLOCK_MCLK_NDIV

/* ------------------------------------------------------------------------
 * HBM PLL (NDIV) overclock
 *
 * Ported from asm64-hooligan's cmpunlocker fork (driver/src/cmpunlock.c).
 * clock = NDIV * 27 MHz.
 *
 * Applied in two halves:
 *  - kgspCmpHbmNdivPostBooterLoad(): per-FBPA PLL cycle while the PLM
 *    gates are fresh and GSP has not run yet (hooked in
 *    kgspBootstrap_TU102, after Booter Load succeeds);
 *  - kgspCmpHbmNdivPostGsp(): multicast COEFF write, in case GSP
 *    reprogrammed the PLL during its own memory init (hooked in
 *    kgspInitRm_IMPL, after kgspStartLogPolling).
 *
 * Both are compiled out unless the build sets CMPUNLOCK_MCLK_NDIV
 * (driver/build.sh generates cmp_hbm_ndiv_cfg.h from --mclk-ndiv=N).
 *
 * Escape hatch for a value that wedges the GPU during driver init:
 *   nvidia.NVreg_RegistryDwords=cmpSafe=1
 * ------------------------------------------------------------------------ */

/* CMP_REG_* register addresses are defined in kernel_gsp.c, in the
 * patch hunk that includes this file (kept there so the manifest gate
 * in tools/read-constants.py finds them in hbm-ndiv.patch). */

#define CMP_FBPA_COUNT              12U
#define CMP_FBPA_STRIDE             0x4000U
#define CMP_FBPA_BASE               0x900000U
#define CMP_FBPA_PLL_CFG_OFFSET     0x3c90U
#define CMP_FBPA_PLL_COEFF_OFFSET   0x3c98U

/* A PRI read that comes back as 0xBAD?????? is an error, not data. */
#define CMP_IS_PRI_ERROR(v)         (((v) & 0xFFF00000U) == 0xBAD00000U)

static NvBool
_kgspCmpHbmNdivSafeMode(OBJGPU *pGpu)
{
    NvU32 data = 0;

    if (osReadRegistryDword(pGpu, "cmpSafe", &data) == NV_OK && data != 0)
    {
        NV_PRINTF(LEVEL_ERROR,
                  "HBMPLL_OC: cmpSafe=1 - skipping HBM PLL overclock\n");
        return NV_TRUE;
    }

    return NV_FALSE;
}

void
kgspCmpHbmNdivPostBooterLoad(OBJGPU *pGpu, KernelGsp *pKernelGsp)
{
    const NvU32 newNdiv = CMPUNLOCK_MCLK_NDIV;
    NvU32 fbpaIdx, pollIdx;
    NvU32 fbpaCount = 0, failCount = 0;
    NvU32 plm0, fbio0, curNdiv;
    NvU32 ddll0, calSt0 = 0, calSt1 = 0;

    if (!_kgspSec2PostblTimingEnabled(pGpu))
        return;

    if (_kgspCmpHbmNdivSafeMode(pGpu))
        return;

    plm0    = GPU_REG_RD32(pGpu, CMP_REG_FBPA_PLL_PLM);
    curNdiv = (GPU_REG_RD32(pGpu, CMP_REG_FBPA_PLL_COEFF) >> 8) & 0xFFU;

    if ((plm0 & 0x10U) == 0)
    {
        NV_PRINTF(LEVEL_ERROR,
                  "HBMPLL_OC: aborted, PLM=0x%08x (bit4 closed)\n", plm0);
        return;
    }

    NV_PRINTF(LEVEL_ERROR,
              "HBMPLL_OC: start NDIV %u->%u (%u->%u MHz) devid=0x%04x vbios=%s PLM=0x%08x\n",
              curNdiv, newNdiv, 27 * curNdiv, 27 * newNdiv,
              pGpu->idInfo.PCIDeviceID >> 16, pKernelGsp->vbiosVersionStr, plm0);

    /* 1. Assert MEMCLK_CHANGE_ALERT (FBIO broadcast bit 31) */
    fbio0 = GPU_REG_RD32(pGpu, CMP_REG_FBIO_BROADCAST);
    GPU_REG_WR32(pGpu, CMP_REG_FBIO_BROADCAST, fbio0 | 0x80000000U);

    /* 2. Enter HBM self-refresh */
    GPU_REG_WR32(pGpu, CMP_REG_HBM_SELF_REFRESH, 0x00000001U);
    osDelay(5);

    /* 3. PLL cycle on each active FBPA */
    for (fbpaIdx = 0; fbpaIdx < CMP_FBPA_COUNT; fbpaIdx++)
    {
        NvU32 base      = CMP_FBPA_BASE + fbpaIdx * CMP_FBPA_STRIDE;
        NvU32 cfgAddr   = base + CMP_FBPA_PLL_CFG_OFFSET;
        NvU32 coeffAddr = base + CMP_FBPA_PLL_COEFF_OFFSET;
        NvU32 origCfg   = GPU_REG_RD32(pGpu, cfgAddr);
        NvU32 origCoeff = GPU_REG_RD32(pGpu, coeffAddr);
        NvU32 newCoeff, lockCfg = 0;

        if (origCfg == 0 || origCoeff == 0 || CMP_IS_PRI_ERROR(origCfg))
            continue;

        fbpaCount++;

        GPU_REG_WR32(pGpu, cfgAddr, origCfg & ~0x09U);
        osDelay(2);

        newCoeff = (origCoeff & ~0xFF00U) | ((newNdiv & 0xFFU) << 8);
        GPU_REG_WR32(pGpu, coeffAddr, newCoeff);

        GPU_REG_WR32(pGpu, cfgAddr, (origCfg | 0x09U) & ~0x20U);

        for (pollIdx = 0; pollIdx < 200; pollIdx++)
        {
            osDelay(1);
            lockCfg = GPU_REG_RD32(pGpu, cfgAddr);
            if (lockCfg & 0x20U)
                break;
        }

        if (!(lockCfg & 0x20U))
        {
            NV_PRINTF(LEVEL_ERROR, "HBMPLL_OC: FBPA%u PLL lock timeout\n", fbpaIdx);
            failCount++;
            continue;
        }

        GPU_REG_WR32(pGpu, cfgAddr, lockCfg & ~0x1000U);
    }

    /* 4. Exit self-refresh */
    GPU_REG_WR32(pGpu, CMP_REG_HBM_SELF_REFRESH, 0x00000000U);
    osDelay(10);

    /* 5. DDLL calibration */
    ddll0 = GPU_REG_RD32(pGpu, CMP_REG_DDLL_CAL);
    GPU_REG_WR32(pGpu, CMP_REG_DDLL_CAL, ddll0 | 0x40U);
    osDelay(10);
    for (pollIdx = 0; pollIdx < 100; pollIdx++)
    {
        osDelay(1);
        calSt0 = GPU_REG_RD32(pGpu, CMP_REG_DDLL_CAL_STATUS_0);
        calSt1 = GPU_REG_RD32(pGpu, CMP_REG_DDLL_CAL_STATUS_1);
    }
    GPU_REG_WR32(pGpu, CMP_REG_DDLL_CAL, ddll0 & ~0x40U);
    NV_PRINTF(LEVEL_ERROR,
              "HBMPLL_OC: DDLL cal status 0x%08x 0x%08x\n", calSt0, calSt1);

    /* 6. Clear MEMCLK_CHANGE_ALERT */
    GPU_REG_WR32(pGpu, CMP_REG_FBIO_BROADCAST,
                 GPU_REG_RD32(pGpu, CMP_REG_FBIO_BROADCAST) & ~0x80000000U);

    if (fbpaCount == 0)
        NV_PRINTF(LEVEL_ERROR, "HBMPLL_OC: no active FBPAs found\n");
    else if (failCount > 0)
        NV_PRINTF(LEVEL_ERROR, "HBMPLL_OC: %u/%u FBPAs failed to lock\n",
                  failCount, fbpaCount);
    else
        NV_PRINTF(LEVEL_ERROR,
                  "HBMPLL_OC: done, %u FBPAs at NDIV=%u (%u MHz)\n",
                  fbpaCount, newNdiv, 27 * newNdiv);
}

void
kgspCmpHbmNdivPostGsp(OBJGPU *pGpu, KernelGsp *pKernelGsp)
{
    const NvU32 newNdiv = CMPUNLOCK_MCLK_NDIV;
    NvU32 coeffPre, plmPre, newCoeff, cfg = 0;
    NvU32 i;

    if (!_kgspSec2PostblTimingEnabled(pGpu))
        return;

    if (_kgspCmpHbmNdivSafeMode(pGpu))
        return;

    coeffPre = GPU_REG_RD32(pGpu, CMP_REG_FBPA_PLL_COEFF);
    plmPre   = GPU_REG_RD32(pGpu, CMP_REG_FBPA_PLL_PLM);

    /* Fall back to the 300W layout if the read came back as a PRI error. */
    if (coeffPre == 0 || CMP_IS_PRI_ERROR(coeffPre))
        newCoeff = (1U << 16) | (newNdiv << 8) | 1U;
    else
        newCoeff = (coeffPre & ~0xFF00U) | ((newNdiv & 0xFFU) << 8);

    NV_PRINTF(LEVEL_ERROR,
              "HBMPLL_OC: post-GSP PRE: COEFF=0x%08x(NDIV=%u) PLM=0x%08x "
              "devid=0x%04x vbios=%s newCOEFF=0x%08x\n",
              coeffPre, (coeffPre >> 8) & 0xFFU, plmPre,
              pGpu->idInfo.PCIDeviceID >> 16, pKernelGsp->vbiosVersionStr, newCoeff);

    GPU_REG_WR32(pGpu, CMP_REG_FBPA_PLL_COEFF_MC, newCoeff);
    GPU_REG_WR32(pGpu, CMP_REG_PRI_FENCE, 0x0U);
    for (i = 0; i < 500; i++)
        (void)GPU_REG_RD32(pGpu, CMP_REG_FBPA_PLL_CFG);

    for (i = 0; i < 200000; i++)
    {
        cfg = GPU_REG_RD32(pGpu, CMP_REG_FBPA_PLL_CFG);
        if (cfg & 0x20U)
            break;
    }

    NV_PRINTF(LEVEL_ERROR,
              "HBMPLL_OC: post-GSP POST: COEFF=0x%08x(NDIV=%u) CFG=0x%08x lock=%u iter=%u\n",
              GPU_REG_RD32(pGpu, CMP_REG_FBPA_PLL_COEFF),
              (GPU_REG_RD32(pGpu, CMP_REG_FBPA_PLL_COEFF) >> 8) & 0xFFU,
              cfg, (cfg >> 5) & 1U, i);
}

#endif /* CMPUNLOCK_MCLK_NDIV */
#endif /* CMP_HBM_NDIV_IMPL_H */
