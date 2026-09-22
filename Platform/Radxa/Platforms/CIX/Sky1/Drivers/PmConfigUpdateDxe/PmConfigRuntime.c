/** @file
  Read the CPU OC admission result for the configuration consumed at boot.
  SPDX-License-Identifier: BSD-2-Clause-Patent
**/

#include <Library/ArmMtlLib.h>
#include <Library/BaseMemoryLib.h>
#include <Library/UefiBootServicesTableLib.h>
#include "PmConfigUpdateDxe.h"
#include "CixCpuOcExpected.h"

#if CIX_CPU_OC_PM_ABI == 5U
// Vendor SCMI protocol 0x80, CPU_OC_STATUS message 3, empty request.
#define PM_RUNTIME_MESSAGE_HEADER  ((0x80U << 10) | 3U)
#define PM_RUNTIME_RESPONSE_WORDS  7U

STATIC UINT32
PmRuntimeWord (IN CONST UINT8 *Buffer, IN UINTN Offset)
{
  UINT32 Value;
  CopyMem (&Value, Buffer + Offset, sizeof (Value));
  return Value;
}

STATIC BOOLEAN
PmDecodeRuntime (IN CONST UINT32 *Reply, IN CONST UINT8 *Config,
                 OUT RADXA_PM_STATUS_DATA *State)
{
  if ((Reply[0] != 0U) || (Reply[1] != 5U) ||
      (Reply[2] > RADXA_PM_RUNTIME_REJECTED) ||
      (Reply[3] > RADXA_PM_REJECT_COUPLED_RANGE) ||
      ((Reply[2] == RADXA_PM_RUNTIME_REJECTED) != (Reply[3] != 0U))) {
    return FALSE;
  }
  if (!PmConfigHeaderIsValid (Config)) {
    return FALSE;
  }
  if (Reply[2] == RADXA_PM_RUNTIME_UNKNOWN) {
    return TRUE;
  }
  State->RuntimeReason = (UINT8)Reply[3];
  // PM captures these authenticated PMCF fields before normalizing its copy.
  // They correlate the boot request; they are not cryptographic attestation.
  if ((Reply[4] != PmRuntimeWord (Config, PM_CONFIG_LENGTH_OFFSET)) ||
      (Reply[5] != PmRuntimeWord (Config, PM_CONFIG_CRC1_OFFSET)) ||
      (Reply[6] != PmRuntimeWord (Config, PM_CONFIG_CRC2_OFFSET))) {
    State->RuntimeState = RADXA_PM_RUNTIME_DIFFERENT_CONFIG;
    return TRUE;
  }
  if (((Reply[2] == RADXA_PM_RUNTIME_ACCEPTED) &&
       (Config[PM_CONFIG_OPP_VALID_OFFSET] != PM_CONFIG_OPP_CPU_OC) &&
       (Config[PM_CONFIG_OPP_VALID_OFFSET] != PM_CONFIG_OPP_CPU_OC_LEGACY)) ||
      ((Reply[2] == RADXA_PM_RUNTIME_INACTIVE) &&
       ((Config[PM_CONFIG_OPP_VALID_OFFSET] == PM_CONFIG_OPP_CPU_OC) ||
        (Config[PM_CONFIG_OPP_VALID_OFFSET] == PM_CONFIG_OPP_CPU_OC_LEGACY)))) {
    State->RuntimeReason = 0U;
    return FALSE;
  }
  State->RuntimeState = (UINT8)Reply[2];
  return TRUE;
}
#endif

VOID
PmReadRuntimeStatus (IN CONST UINT8 *Config, IN OUT RADXA_PM_STATUS_DATA *State)
{
#if CIX_CPU_OC_PM_ABI == 5U
  EFI_STATUS Status;
  EFI_TPL OldTpl;
  MTL_CHANNEL *Channel;
  UINT32 Header;
  UINT32 Length;
  UINT32 Reply[PM_RUNTIME_RESPONSE_WORDS];
#endif

  State->RuntimeState = RADXA_PM_RUNTIME_UNKNOWN;
  State->RuntimeReason = 0U;
#if CIX_CPU_OC_PM_ABI == 5U
  if (!State->CustomSupported) {
    return;
  }
  // Serialize this polling transaction against other DXE event callbacks.
  // Use the platform MTL binding, including its existing timeout behavior.
  OldTpl = gBS->RaiseTPL (TPL_NOTIFY);
  Status = MtlGetChannel (MTL_CHANNEL_TYPE_LOW, &Channel);
  if (!EFI_ERROR (Status)) {
    Status = MtlWaitUntilChannelFree (Channel, 20000U);
  }
  if (!EFI_ERROR (Status)) {
    Status = MtlSendMessage (Channel, PM_RUNTIME_MESSAGE_HEADER, 0U);
  }
  if (!EFI_ERROR (Status)) {
    // The pinned binary MTL receive routine has a much longer internal wait.
    // Bound this diagnostic query before entering that routine.
    Status = MtlWaitUntilChannelFree (Channel, 20000U);
  }
  if (!EFI_ERROR (Status)) {
    Status = MtlReceiveMessage (Channel, &Header, &Length);
  }
  if (!EFI_ERROR (Status) && (Header == PM_RUNTIME_MESSAGE_HEADER) &&
      (Length == sizeof (Reply))) {
    CopyMem (Reply, MtlGetChannelPayload (Channel), sizeof (Reply));
    PmDecodeRuntime (Reply, Config, State);
  }
  gBS->RestoreTPL (OldTpl);
#else
  (VOID)Config;
#endif
}
