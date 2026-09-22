/** @file
  Apply a versioned CPU-only profile after validating the installed PM consumer.
  SPDX-License-Identifier: BSD-2-Clause-Patent
**/
#include "PmConfigUpdateDxe.h"
#include "CixCpuOcExpected.h"
#include <Protocol/CixFwUpdateProtocol.h>
#include <Library/BaseCryptLib.h>
#include <Library/BaseLib.h>
#include <Library/BaseMemoryLib.h>
#include <Library/DebugLib.h>
#include <Library/MemoryAllocationLib.h>
#include <Library/UefiBootServicesTableLib.h>
#include <Library/UefiRuntimeServicesTableLib.h>

#define PM_VARIABLE_ATTRIBUTES (EFI_VARIABLE_NON_VOLATILE | EFI_VARIABLE_BOOTSERVICE_ACCESS)
#define PM_STATUS_ATTRIBUTES EFI_VARIABLE_BOOTSERVICE_ACCESS
#define PM_BL1_MAX_SIZE (1024U * 1024U)

STATIC EFI_STATUS
PmStoreSettings (IN CONST RADXA_PM_TUNING_DATA *Settings)
{
  return gRT->SetVariable (RADXA_PM_TUNING_VAR, &gRadxaSetupVariableGuid,
                          PM_VARIABLE_ATTRIBUTES, sizeof (*Settings), (VOID *)Settings);
}

STATIC EFI_STATUS
PmGetSettings (OUT RADXA_PM_TUNING_DATA *Settings)
{
  EFI_STATUS Status;
  UINTN Size;
  UINT32 Attributes;

  PmInitializeSettings (Settings);
  Size = sizeof (*Settings);
  Status = gRT->GetVariable (RADXA_PM_TUNING_VAR, &gRadxaSetupVariableGuid,
                            &Attributes, &Size, Settings);
  if (Status == EFI_NOT_FOUND) {
    return PmStoreSettings (Settings);
  }
  if (EFI_ERROR (Status)) {
    return Status;
  }
  if (Attributes != PM_VARIABLE_ATTRIBUTES) {
    return EFI_COMPROMISED_DATA;
  }
  if (Size == RADXA_PM_TUNING_V1_SIZE) {
    if (!PmMigrateLegacySettings (Settings)) {
      return EFI_COMPROMISED_DATA;
    }
    return PmStoreSettings (Settings);
  }
  // Foreign, partial and future records remain untouched. Only an exact,
  // validated revision-1 record is eligible for migration under the same name.
  if (Size != sizeof (*Settings)) {
    return EFI_COMPROMISED_DATA;
  }
  return PmSettingsAreValid (Settings) ? EFI_SUCCESS : EFI_COMPROMISED_DATA;
}

STATIC EFI_STATUS
PmPublishStatus (IN CONST RADXA_PM_STATUS_DATA *NewStatus)
{
  RADXA_PM_STATUS_DATA Previous;
  EFI_STATUS Status;
  UINTN Size;

  Size = sizeof (Previous);
  Status = gRT->GetVariable (RADXA_PM_STATUS_VAR, &gRadxaSetupVariableGuid,
                            NULL, &Size, &Previous);
  if (!EFI_ERROR (Status) && (Size == sizeof (Previous)) &&
      (CompareMem (&Previous, NewStatus, Size) == 0)) {
    return EFI_SUCCESS;
  }
  return gRT->SetVariable (RADXA_PM_STATUS_VAR, &gRadxaSetupVariableGuid,
                          PM_STATUS_ATTRIBUTES, sizeof (*NewStatus), (VOID *)NewStatus);
}

#if CIX_CPU_OC_PM_ABI == 5U
STATIC UINT32
PmBl1Read32 (IN CONST UINT8 *Buffer, IN UINTN Offset)
{
  UINT32 Value;
  CopyMem (&Value, Buffer + Offset, sizeof (Value));
  return Value;
}

STATIC BOOLEAN
PmBl1SelectsExpectedPm (IN CONST UINT8 *Buffer)
{
  UINT32 Index;
  UINT32 Previous;
  UINT32 Entry;
  UINT32 Id;
  UINT32 Offset;
  UINT32 Size;
  UINT32 Seen;
  UINT32 Offsets[3];
  UINT32 Sizes[3];

  if ((CIX_CPU_OC_BL1_SIZE < 4096U) ||
      (CompareMem (Buffer, "CIXBTFF!", 8U) != 0) ||
      (PmBl1Read32 (Buffer, 12U) != 0U) ||
      (PmBl1Read32 (Buffer, 16U) < 1U) || (PmBl1Read32 (Buffer, 16U) > 2U) ||
      (PmBl1Read32 (Buffer, 2048U) < 1U) || (PmBl1Read32 (Buffer, 2048U) > 30U) ||
      ((PmBl1Read32 (Buffer, 2532U) != 0x5AU) &&
       (PmBl1Read32 (Buffer, 2532U) != 0x5CU)) ||
      (PmBl1Read32 (Buffer, 2536U) != 3U)) {
    return FALSE;
  }
  Seen = 0U;
  for (Index = 0U; Index < 3U; Index++) {
    Entry = 2540U + Index * 24U;
    Id = PmBl1Read32 (Buffer, Entry);
    Offset = PmBl1Read32 (Buffer, Entry + 12U);
    Size = PmBl1Read32 (Buffer, Entry + 16U);
    if ((Id < 1U) || (Id > 3U) || ((Seen & (1U << Id)) != 0U) ||
        (Offset < 4096U) || ((Offset % 4096U) != 0U) ||
        (Offset >= CIX_CPU_OC_BL1_SIZE) || (Size == 0U) ||
        (Size > CIX_CPU_OC_BL1_SIZE - Offset)) {
      return FALSE;
    }
    for (Previous = 0U; Previous < Index; Previous++) {
      if ((Offset < Offsets[Previous] + Sizes[Previous]) &&
          (Offsets[Previous] < Offset + Size)) {
        return FALSE;
      }
    }
    Offsets[Index] = Offset;
    Sizes[Index] = Size;
    Seen |= 1U << Id;
    if ((Id == 2U) && ((Offset != CIX_CPU_OC_PM_OFFSET) ||
                       (Size != CIX_CPU_OC_PM_SIZE))) {
      return FALSE;
    }
  }
  return Seen == 0x0EU;
}
#endif

STATIC BOOLEAN
PmConsumerSupported (IN CIX_FW_UPDATE_PROTOCOL *FwUpdate)
{
#if CIX_CPU_OC_PM_ABI == 5U
  STATIC CONST UINT8 ExpectedHash[SHA256_DIGEST_SIZE] = CIX_CPU_OC_PM_SHA256;
  UINT8 Digest[SHA256_DIGEST_SIZE];
  UINT8 *Bootloader;
  UINT16 Result;
  BOOLEAN Valid;

  if ((FwUpdate->Version < 3U) || (FwUpdate->FirmwareRawEntryUpdate == NULL) ||
      (CIX_CPU_OC_BL1_SIZE == 0U) ||
      (CIX_CPU_OC_BL1_SIZE > PM_BL1_MAX_SIZE) || (CIX_CPU_OC_PM_SIZE == 0U) ||
      (CIX_CPU_OC_PM_OFFSET >= CIX_CPU_OC_BL1_SIZE) ||
      (CIX_CPU_OC_PM_SIZE > CIX_CPU_OC_BL1_SIZE - CIX_CPU_OC_PM_OFFSET)) {
    return FALSE;
  }
  Bootloader = AllocateZeroPool (CIX_CPU_OC_BL1_SIZE);
  if (Bootloader == NULL) {
    return FALSE;
  }
  Result = FwUpdate->FirmwareRawEntryUpdate (FIRMWARE_TYPE_BootLoader_1,
             Bootloader, CIX_CPU_OC_BL1_SIZE, ENTRY_READ, NULL);
  Valid = ((Result & 0xFFU) == FIRMWARE_RET_SUCCESS) &&
          PmBl1SelectsExpectedPm (Bootloader) &&
          Sha256HashAll (Bootloader + CIX_CPU_OC_PM_OFFSET,
                         CIX_CPU_OC_PM_SIZE, Digest) &&
          (CompareMem (Digest, ExpectedHash, sizeof (Digest)) == 0);
  FreePool (Bootloader);
  return Valid;
#else
  // Stock, older ABI and unidentified consumers cannot apply the ABI5 profile.
  (VOID)FwUpdate;
  return FALSE;
#endif
}

STATIC UINT8
PmReadFlash (IN CIX_FW_UPDATE_PROTOCOL *FwUpdate, OUT UINT8 *Buffer)
{
  UINT16 Result;
  ZeroMem (Buffer, PM_CONFIG_BIN_SIZE);
  Result = FwUpdate->FirmwareRawEntryUpdate (FIRMWARE_TYPE_PM_CONF,
             Buffer, PM_CONFIG_BIN_SIZE, ENTRY_READ, NULL);
  if ((Result & 0xFFU) != FIRMWARE_RET_SUCCESS) {
    return RADXA_PM_ERROR_READ;
  }
  return PmConfigHeaderIsValid (Buffer) ? RADXA_PM_ERROR_NONE : RADXA_PM_ERROR_CONFIG;
}

EFI_STATUS EFIAPI
PmConfigUpdateDxeEntryPoint (IN EFI_HANDLE ImageHandle, IN EFI_SYSTEM_TABLE *SystemTable)
{
  EFI_STATUS Status;
  CIX_FW_UPDATE_PROTOCOL *FwUpdate;
  RADXA_PM_TUNING_DATA Settings;
  RADXA_PM_STATUS_DATA State;
  UINT8 *Current;
  UINT8 *Expected;
  UINT8 *Verify;
  UINT8 Error;
  UINT16 Result;

  Current = NULL;
  Expected = NULL;
  Verify = NULL;
  ZeroMem (&State, sizeof (State));
  State.Revision = 2U;
  State.SavedProfile = RADXA_PM_SAVED_UNKNOWN;
  State.State = RADXA_PM_STATE_FAILED;
  Status = gBS->LocateProtocol (&gCixFirmwareUpdateProtocolGuid, NULL, (VOID **)&FwUpdate);
  if (EFI_ERROR (Status)) {
    State.LastError = RADXA_PM_ERROR_READ;
    goto Done;
  }
  if ((FwUpdate->Version < 3U) || (FwUpdate->FirmwareRawEntryUpdate == NULL)) {
    State.LastError = RADXA_PM_ERROR_UNSUPPORTED;
    goto Done;
  }
  State.CustomSupported = PmConsumerSupported (FwUpdate);
  // Reserve all read/modify/verify resources before the first flash mutation.
  Current = AllocateZeroPool (PM_CONFIG_BIN_SIZE);
  Expected = AllocateZeroPool (PM_CONFIG_BIN_SIZE);
  Verify = AllocateZeroPool (PM_CONFIG_BIN_SIZE);
  if ((Current == NULL) || (Expected == NULL) || (Verify == NULL)) {
    State.LastError = RADXA_PM_ERROR_MEMORY;
    goto Done;
  }
  Error = PmReadFlash (FwUpdate, Current);
  if (Error != RADXA_PM_ERROR_NONE) {
    State.LastError = Error;
    goto Done;
  }
  if (PmValidateConfig (Current)) {
    State.SavedProfile = PmSavedProfile (Current);
  }
  PmReadRuntimeStatus (Current, &State);
  Status = PmGetSettings (&Settings);
  if (EFI_ERROR (Status)) {
    State.LastError = RADXA_PM_ERROR_SETTINGS;
    goto Done;
  }
  // The default Vendor request must not disable somebody else's full/partial
  // external table. Only the native or this feature's CPU-only format is owned.
  if (!PmConfigIsCpuOwned (Current)) {
    State.LastError = RADXA_PM_ERROR_FOREIGN;
    goto Done;
  }
  if ((Settings.Profile == RADXA_PM_PROFILE_CUSTOM) && !State.CustomSupported) {
    State.LastError = RADXA_PM_ERROR_UNSUPPORTED;
    goto Done;
  }
  if ((Settings.Profile == RADXA_PM_PROFILE_CUSTOM) &&
      !PmValidateConfig (Current)) {
    State.LastError = RADXA_PM_ERROR_CONFIG;
    goto Done;
  }
  if (PmValidateConfig (Current) && PmProfileMatches (Current, &Settings)) {
    State.State = RADXA_PM_STATE_READY;
    goto Done;
  }
  CopyMem (Expected, Current, PM_CONFIG_BIN_SIZE);
  if (!PmApplyProfile (Expected, &Settings) ||
      !PmValidateConfig (Expected) || !PmProfileMatches (Expected, &Settings)) {
    State.LastError = RADXA_PM_ERROR_CONFIG;
    goto Done;
  }
  State.State = RADXA_PM_STATE_RESET_PENDING;
  Status = PmPublishStatus (&State);
  if (EFI_ERROR (Status)) {
    State.State = RADXA_PM_STATE_FAILED;
    State.LastError = RADXA_PM_ERROR_VARIABLE;
    goto Done;
  }
  Result = FwUpdate->FirmwareRawEntryUpdate (FIRMWARE_TYPE_PM_CONF,
             Expected, PM_CONFIG_BIN_SIZE, ENTRY_WRITE, NULL);
  if ((Result & 0xFFU) != FIRMWARE_RET_SUCCESS) {
    State.LastError = RADXA_PM_ERROR_WRITE;
    goto WriteFailed;
  }
  Error = PmReadFlash (FwUpdate, Verify);
  if ((Error != RADXA_PM_ERROR_NONE) ||
      (CompareMem (Verify, Expected, PM_CONFIG_BIN_SIZE) != 0)) {
    State.LastError = RADXA_PM_ERROR_VERIFY;
    goto WriteFailed;
  }
  // The sector contains the request; PM may still reject it on the next boot.
  // Persisted bytes do not prove runtime application or voltage.
  FreePool (Current);
  FreePool (Expected);
  FreePool (Verify);
  gRT->ResetSystem (EfiResetCold, EFI_SUCCESS, 0, NULL);
  CpuDeadLoop ();
  return EFI_DEVICE_ERROR;

WriteFailed:
  State.State = RADXA_PM_STATE_FAILED;
  // A partial write has unknown persistence; do not claim the requested state
  // is saved and do not attempt an unvalidated second write as "rollback".
  State.SavedProfile = RADXA_PM_SAVED_UNKNOWN;
Done:
  Status = PmPublishStatus (&State);
  DEBUG ((DEBUG_INFO, "[CpuPm] saved=%u supported=%u state=%u error=%u runtime=%u reason=%u status=%r\n",
          State.SavedProfile, State.CustomSupported, State.State, State.LastError,
          State.RuntimeState, State.RuntimeReason, Status));
  if (Current != NULL) { FreePool (Current); }
  if (Expected != NULL) { FreePool (Expected); }
  if (Verify != NULL) { FreePool (Verify); }
  return Status;
}
