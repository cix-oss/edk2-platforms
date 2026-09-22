/** @file
  Update the CIX v3 PM configuration from the selected BIOS profile.

  The PM firmware consumes the dedicated 4 KiB PM configuration before UEFI
  starts. This DXE driver therefore updates that region, verifies the write,
  and requests a cold reset so the selected profile is consumed on next boot.

  SPDX-License-Identifier: BSD-2-Clause-Patent
**/

#include "PmConfigUpdateDxe.h"

#include <Library/BaseLib.h>
#include <Library/BaseMemoryLib.h>
#include <Library/DebugLib.h>

STATIC_ASSERT (sizeof (RADXA_PM_TUNING_DATA) == 278U, "CPU tuning revision-2 size");
STATIC_ASSERT (OFFSET_OF (RADXA_PM_TUNING_DATA, LittleMode) == RADXA_PM_TUNING_V1_SIZE,
               "CPU tuning revision-1 prefix must stay intact");

typedef struct {
  CONST CHAR8    *Name;
  UINT8          Domain;
  UINT8          Size;
  UINT8          SustainedIndex;
  UINT8          ProtectedIndex;
  UINT16         Frequency[PM_CONFIG_OPP_ENTRY_COUNT];
  UINT16         Voltage[PM_CONFIG_OPP_ENTRY_COUNT];
  UINT16         MeasuredFrequency[PM_CONFIG_MEASURED_POINT_COUNT];
  UINT16         MeasuredPower[PM_CONFIG_MEASURED_POINT_COUNT];
} PM_CPU_DOMAIN_DATA;

STATIC CONST PM_CPU_DOMAIN_DATA  mCpuDomains[PM_CONFIG_CPU_DOMAIN_COUNT] = {
  {
    "GB0", 3, 7, 2, PM_CONFIG_CPU_PROTECTED_INDEX,
    { 800, 1200, 1500, 1800, 2200, 2400, 2500 },
    { 750, 750, 790, 790, 790, 850, 920 },
    { 2500, 2400, 2200, 1500, 1200, 800, 100 },
    { 5000, 4500, 3500, 2000, 1600, 500, 50 }
  },
  {
    "GB1", 4, 7, 2, PM_CONFIG_CPU_PROTECTED_INDEX,
    { 800, 1200, 1500, 1800, 2200, 2500, 2600 },
    { 750, 750, 790, 790, 790, 850, 920 },
    { 2600, 2500, 2200, 1500, 1200, 800, 100 },
    { 5100, 4700, 3600, 2000, 1600, 500, 50 }
  },
  {
    "GM0", 5, 7, 2, PM_CONFIG_CPU_PROTECTED_INDEX,
    { 800, 1200, 1500, 1800, 2100, 2200, 2300 },
    { 750, 750, 790, 790, 790, 850, 890 },
    { 2300, 2200, 2100, 1500, 1200, 800, 100 },
    { 3500, 3200, 3000, 1900, 1500, 500, 50 }
  },
  {
    "GM1", 6, 6, 2, PM_CONFIG_CPU_PROTECTED_INDEX,
    { 800, 1200, 1500, 1800, 2100, 2200 },
    { 750, 750, 790, 790, 850, 890 },
    { 2200, 2100, 1800, 1500, 1200, 800, 100 },
    { 3500, 3300, 2800, 1900, 1500, 500, 50 }
  }
};

STATIC
UINT16
PmRead16 (
  IN CONST UINT8  *Buffer,
  IN UINTN        Offset
  )
{
  return (UINT16)(Buffer[Offset] | ((UINT16)Buffer[Offset + 1U] << 8));
}

STATIC
UINT32
PmRead32 (
  IN CONST UINT8  *Buffer,
  IN UINTN        Offset
  )
{
  return (UINT32)Buffer[Offset] |
         ((UINT32)Buffer[Offset + 1U] << 8) |
         ((UINT32)Buffer[Offset + 2U] << 16) |
         ((UINT32)Buffer[Offset + 3U] << 24);
}

STATIC
VOID
PmWrite16 (
  IN OUT UINT8  *Buffer,
  IN UINTN      Offset,
  IN UINT16     Value
  )
{
  Buffer[Offset]      = (UINT8)Value;
  Buffer[Offset + 1U] = (UINT8)(Value >> 8);
}

STATIC
VOID
PmWrite32 (
  IN OUT UINT8  *Buffer,
  IN UINTN      Offset,
  IN UINT32     Value
  )
{
  Buffer[Offset]      = (UINT8)Value;
  Buffer[Offset + 1U] = (UINT8)(Value >> 8);
  Buffer[Offset + 2U] = (UINT8)(Value >> 16);
  Buffer[Offset + 3U] = (UINT8)(Value >> 24);
}

STATIC
UINTN
PmSettingsIndex (
  IN UINTN  CpuDomain,
  IN UINTN  Opp
  )
{
  return (CpuDomain * RADXA_PM_OPP_COUNT) + Opp;
}

STATIC
UINT32
PmMeasuredPower (
  IN CONST PM_CPU_DOMAIN_DATA  *Domain,
  IN UINT16                    Frequency
  )
{
  UINT32  FrequencyDelta;
  UINT32  Index;
  UINT32  LowerFrequency;
  UINT32  LowerPower;
  UINT32  PowerDelta;

  for (Index = 0; Index < PM_CONFIG_MEASURED_POINT_COUNT; Index++) {
    if (Domain->MeasuredFrequency[Index] == Frequency) {
      return Domain->MeasuredPower[Index];
    }

    if (Domain->MeasuredFrequency[Index] < Frequency) {
      break;
    }
  }

  if (Index == PM_CONFIG_MEASURED_POINT_COUNT) {
    Index--;
  } else if (Index == 0) {
    Index++;
  }

  LowerFrequency = Domain->MeasuredFrequency[Index];
  LowerPower     = Domain->MeasuredPower[Index];
  FrequencyDelta = Domain->MeasuredFrequency[Index - 1] - LowerFrequency;
  PowerDelta     = Domain->MeasuredPower[Index - 1] - LowerPower;

  return LowerPower +
         (PowerDelta * (Frequency - LowerFrequency)) / FrequencyDelta;
}

STATIC
UINT16
PmReferenceVoltage (
  IN CONST PM_CPU_DOMAIN_DATA  *Domain,
  IN UINT16                    Frequency
  )
{
  UINT32  FrequencyDelta;
  UINT32  Index;
  UINT32  LowerFrequency;
  UINT32  LowerVoltage;
  UINT32  VoltageDelta;

  if (Frequency <= Domain->Frequency[0]) {
    return Domain->Voltage[0];
  }

  for (Index = 1; Index < Domain->Size; Index++) {
    if (Frequency <= Domain->Frequency[Index]) {
      LowerFrequency = Domain->Frequency[Index - 1];
      LowerVoltage   = Domain->Voltage[Index - 1];
      FrequencyDelta = Domain->Frequency[Index] - LowerFrequency;
      VoltageDelta   = Domain->Voltage[Index] - LowerVoltage;
      return (UINT16)(LowerVoltage +
                      (VoltageDelta * (Frequency - LowerFrequency)) /
                      FrequencyDelta);
    }
  }

  return Domain->Voltage[Domain->Size - 1];
}

STATIC
UINT32
PmEstimatedPower (
  IN CONST PM_CPU_DOMAIN_DATA  *Domain,
  IN UINT16                    Frequency,
  IN UINT16                    Voltage
  )
{
  UINT64  Denominator;
  UINT64  Estimate;
  UINT64  Numerator;
  UINT64  Remainder;
  UINT32  BasePower;
  UINT32  PowerVoltage;
  UINT32  ReferenceVoltage;

  BasePower       = PmMeasuredPower (Domain, Frequency);
  ReferenceVoltage = PmReferenceVoltage (Domain, Frequency);
  PowerVoltage    = Voltage;
  if (PowerVoltage <= ReferenceVoltage) {
    return BasePower;
  }

  Numerator = MultU64x32 (
                MultU64x32 ((UINT64)BasePower, PowerVoltage),
                PowerVoltage
                );
  Denominator = MultU64x32 (
                  (UINT64)ReferenceVoltage,
                  ReferenceVoltage
                  );
  Estimate = DivU64x64Remainder (Numerator, Denominator, &Remainder);
  if (Remainder != 0) {
    Estimate++;
  }

  return (UINT32)Estimate;
}

STATIC
VOID
PmCalculateChecksum (
  IN CONST UINT8  *Buffer,
  IN UINT32       Length,
  OUT UINT32      *Crc1,
  OUT UINT32      *Crc2
  )
{
  UINT32  Cka;
  UINT32  Ckb;
  UINT32  Offset;
  UINT32  Word;

  Cka = 0;
  Ckb = 0;
  for (Offset = 0; Offset < Length; Offset += sizeof (UINT32)) {
    if ((Offset == PM_CONFIG_CRC1_OFFSET) ||
        (Offset == PM_CONFIG_CRC2_OFFSET))
    {
      Word = 0;
    } else {
      Word = PmRead32 (Buffer, Offset);
    }

    Cka += Word;
    Ckb += Cka;
  }

  *Crc1 = Cka;
  *Crc2 = Ckb;
}

VOID
PmInitializeSettings (
  OUT RADXA_PM_TUNING_DATA  *Settings
  )
{
  UINTN  CpuDomain;
  UINTN  Opp;
  UINTN  Index;

  ZeroMem (Settings, sizeof (*Settings));
  Settings->Profile   = RADXA_PM_PROFILE_VENDOR;
  Settings->Revision  = RADXA_PM_TUNING_REVISION;
  Settings->DataSize  = sizeof (*Settings);
  Settings->Signature = RADXA_PM_TUNING_SIGNATURE;
  Settings->LittleMode = RADXA_PM_LITTLE_NATIVE;
  Settings->LittleMaxFrequency = PM_CONFIG_LITTLE_FREQUENCY_MIN;
  Settings->LittleMinVoltage = 0U;
  for (CpuDomain = 0; CpuDomain < ARRAY_SIZE (mCpuDomains); CpuDomain++) {
    for (Opp = 0; Opp < mCpuDomains[CpuDomain].Size; Opp++) {
      Index = PmSettingsIndex (CpuDomain, Opp);
      Settings->CpuFrequency[Index] = mCpuDomains[CpuDomain].Frequency[Opp];
      Settings->CpuVoltage[Index]   = mCpuDomains[CpuDomain].Voltage[Opp];
      Settings->CpuVoltageMode[Index] = RADXA_PM_VOLTAGE_FIXED;
    }
  }
}

STATIC
BOOLEAN
PmCustomSettingsAreValid (
  IN CONST RADXA_PM_TUNING_DATA  *Settings,
  IN UINT16                       MidMaximum
  )
{
  CONST PM_CPU_DOMAIN_DATA  *Domain;
  UINT16                    Frequency;
  UINT16                    PreviousFrequency;
  UINT16                    PreviousVoltage;
  UINT16                    Voltage;
  UINT8                     VoltageMode;
  UINTN                     CpuDomain;
  UINTN                     Index;
  UINTN                     Opp;

  for (CpuDomain = 0; CpuDomain < ARRAY_SIZE (mCpuDomains); CpuDomain++) {
    Domain            = &mCpuDomains[CpuDomain];
    PreviousFrequency = 0;
    PreviousVoltage   = 0;
    for (Opp = 0; Opp < Domain->Size; Opp++) {
      Index       = PmSettingsIndex (CpuDomain, Opp);
      Frequency   = Settings->CpuFrequency[Index];
      Voltage     = Settings->CpuVoltage[Index];
      VoltageMode = Settings->CpuVoltageMode[Index];

      if ((Frequency < PM_CONFIG_CPU_FREQUENCY_MIN) ||
          (Frequency > ((CpuDomain < 2) ? PM_CONFIG_CPU_FREQUENCY_MAX : MidMaximum)) ||
          ((Frequency % 10U) != 0) ||
          (Voltage < PM_CONFIG_CPU_VOLTAGE_MIN) ||
          (Voltage > PM_CONFIG_CPU_VOLTAGE_MAX) ||
          ((Voltage % 10U) != 0) ||
          (VoltageMode != RADXA_PM_VOLTAGE_FIXED))
      {
        DEBUG ((
          DEBUG_ERROR,
          "[PmConfigUpdate] %a OPP %u is outside the custom limits\n",
          Domain->Name,
          (UINT32)Opp
          ));
        return FALSE;
      }

      if ((Opp > 0) &&
          ((Frequency <= PreviousFrequency) || (Voltage < PreviousVoltage)))
      {
        DEBUG ((
          DEBUG_ERROR,
          "[PmConfigUpdate] %a OPP table is not monotonic at index %u\n",
          Domain->Name,
          (UINT32)Opp
          ));
        return FALSE;
      }

      if ((Opp == Domain->ProtectedIndex) &&
          ((Frequency != PM_CONFIG_CPU_BOOT_LEVEL) ||
           (Voltage != PM_CONFIG_CPU_BOOT_VOLTAGE) ||
           (VoltageMode != RADXA_PM_VOLTAGE_FIXED)))
      {
        DEBUG ((
          DEBUG_ERROR,
          "[PmConfigUpdate] %a boot OPP is not editable\n",
          Domain->Name
          ));
        return FALSE;
      }

      PreviousFrequency = Frequency;
      PreviousVoltage   = Voltage;
    }
    if (Settings->Reserved[CpuDomain] != 0U) {
      return FALSE;
    }
    for (Opp = Domain->Size; Opp < RADXA_PM_OPP_COUNT; Opp++) {
      Index = PmSettingsIndex (CpuDomain, Opp);
      if ((Settings->CpuFrequency[Index] != 0U) ||
          (Settings->CpuVoltage[Index] != 0U) ||
          (Settings->CpuVoltageMode[Index] != RADXA_PM_VOLTAGE_FIXED)) {
        return FALSE;
      }
    }
  }

  return TRUE;
}

STATIC
BOOLEAN
PmLittleRequestIsValid (
  IN UINT32  Frequency,
  IN UINT32  Voltage
  )
{
  return (Frequency >= PM_CONFIG_LITTLE_FREQUENCY_MIN) &&
         (Frequency <= PM_CONFIG_LITTLE_FREQUENCY_MAX) &&
         ((Frequency % 10U) == 0U) &&
         ((Voltage == 0U) ||
          ((Voltage >= PM_CONFIG_LITTLE_VOLTAGE_MIN) &&
           (Voltage <= PM_CONFIG_LITTLE_VOLTAGE_MAX) &&
           ((Voltage % 10U) == 0U)));
}

BOOLEAN
PmMigrateLegacySettings (
  IN OUT RADXA_PM_TUNING_DATA  *Settings
  )
{
  // The caller has already checked the exact old size and attributes. Do not
  // touch any bytes until the revision-1 record is valid under its old policy.
  // Vendor ignored inactive BIG/MID values; preserve those bytes so migration
  // cannot prevent restoring Vendor after an invalid custom edit. Switching
  // back to Custom still validates every active value using the new policy.
  if ((Settings->Revision != 1U) ||
      (Settings->DataSize != RADXA_PM_TUNING_V1_SIZE) ||
      (Settings->Signature != RADXA_PM_TUNING_SIGNATURE) ||
      ((Settings->Profile != RADXA_PM_PROFILE_VENDOR) &&
       (Settings->Profile != RADXA_PM_PROFILE_CUSTOM)) ||
      ((Settings->Profile == RADXA_PM_PROFILE_CUSTOM) &&
       !PmCustomSettingsAreValid (Settings, 2600U))) {
    return FALSE;
  }
  Settings->Revision = RADXA_PM_TUNING_REVISION;
  Settings->DataSize = sizeof (*Settings);
  Settings->LittleMode = RADXA_PM_LITTLE_NATIVE;
  Settings->LittleMaxFrequency = PM_CONFIG_LITTLE_FREQUENCY_MIN;
  Settings->LittleMinVoltage = 0U;
  return TRUE;
}

BOOLEAN
PmSettingsAreValid (
  IN CONST RADXA_PM_TUNING_DATA  *Settings
  )
{
  if ((Settings->Revision != RADXA_PM_TUNING_REVISION) ||
      (Settings->DataSize != sizeof (*Settings)) ||
      (Settings->Signature != RADXA_PM_TUNING_SIGNATURE))
  {
    DEBUG ((DEBUG_ERROR, "[PmConfigUpdate] Invalid PM tuning variable header\n"));
    return FALSE;
  }

  if ((Settings->Profile != RADXA_PM_PROFILE_VENDOR) &&
      (Settings->Profile != RADXA_PM_PROFILE_CUSTOM))
  {
    DEBUG ((DEBUG_ERROR, "[PmConfigUpdate] Unknown PM profile\n"));
    return FALSE;
  }

  if (((Settings->LittleMode != RADXA_PM_LITTLE_NATIVE) &&
       (Settings->LittleMode != RADXA_PM_LITTLE_CUSTOM)) ||
      !PmLittleRequestIsValid (Settings->LittleMaxFrequency,
                               Settings->LittleMinVoltage)) {
    return FALSE;
  }
  return ((Settings->Profile != RADXA_PM_PROFILE_CUSTOM) ||
          PmCustomSettingsAreValid (Settings, PM_CONFIG_CPU_FREQUENCY_MAX));
}

STATIC
BOOLEAN
PmLittleDescriptorIsValid (
  IN CONST UINT8  *Buffer
  )
{
  UINTN  Offset;
  UINTN  EntryOffset;
  UINT32 Frequency;
  BOOLEAN Erased;

  Erased = TRUE;
  for (Offset = PM_CONFIG_OPP_DOMAIN_OFFSET (PM_CONFIG_LITTLE_DOMAIN);
       Offset < PM_CONFIG_OPP_DOMAIN_OFFSET (PM_CONFIG_LITTLE_DOMAIN) + PM_CONFIG_OPP_DOMAIN_SIZE;
       Offset++) {
    if (Buffer[Offset] != 0xFFU) {
      Erased = FALSE;
      break;
    }
  }
  if (Erased) {
    return TRUE;
  }
  if ((PmRead16 (Buffer, PM_CONFIG_OPP_DOMAIN_OFFSET (PM_CONFIG_LITTLE_DOMAIN)) != 1U) ||
      (PmRead16 (Buffer, PM_CONFIG_OPP_DOMAIN_OFFSET (PM_CONFIG_LITTLE_DOMAIN) + 2U) != 0U)) {
    return FALSE;
  }
  EntryOffset = PM_CONFIG_OPP_ENTRY_OFFSET (PM_CONFIG_LITTLE_DOMAIN, 0U);
  Frequency = PmRead32 (Buffer, EntryOffset + PM_CONFIG_OPP_LEVEL_OFFSET);
  if (!PmLittleRequestIsValid (Frequency,
                               PmRead32 (Buffer, EntryOffset + PM_CONFIG_OPP_VOLTAGE_OFFSET)) ||
      (PmRead32 (Buffer, EntryOffset + PM_CONFIG_OPP_FREQUENCY_OFFSET) != Frequency * 1000U) ||
      (PmRead32 (Buffer, EntryOffset + PM_CONFIG_OPP_POWER_OFFSET) != 0U)) {
    return FALSE;
  }
  for (Offset = EntryOffset + PM_CONFIG_OPP_ENTRY_SIZE;
       Offset < PM_CONFIG_OPP_DOMAIN_OFFSET (PM_CONFIG_LITTLE_DOMAIN) + PM_CONFIG_OPP_DOMAIN_SIZE;
       Offset++) {
    if (Buffer[Offset] != 0xFFU) {
      return FALSE;
    }
  }
  return TRUE;
}

STATIC
BOOLEAN
PmNonCpuDomainsAreUnused (
  IN CONST UINT8  *Buffer
  )
{
  UINTN  Domain;
  UINTN  Offset;

  for (Domain = 0; Domain < PM_CONFIG_OPP_DOMAIN_COUNT; Domain++) {
    if ((Domain == PM_CONFIG_LITTLE_DOMAIN) &&
        (Buffer[PM_CONFIG_OPP_VALID_OFFSET] == PM_CONFIG_OPP_CPU_OC)) {
      // Claim only our one-entry request descriptor, never a foreign LITTLE table.
      if (!PmLittleDescriptorIsValid (Buffer)) {
        return FALSE;
      }
      continue;
    }
    if ((Domain >= mCpuDomains[0].Domain) &&
        (Domain <= mCpuDomains[PM_CONFIG_CPU_DOMAIN_COUNT - 1U].Domain))
    {
      continue;
    }

    for (Offset = PM_CONFIG_OPP_DOMAIN_OFFSET (Domain);
         Offset < PM_CONFIG_OPP_DOMAIN_OFFSET (Domain) + PM_CONFIG_OPP_DOMAIN_SIZE;
         Offset++)
    {
      if (Buffer[Offset] != 0xFFU) {
        DEBUG ((
          DEBUG_ERROR,
          "[PmConfigUpdate] External non-CPU domain %u is configured\n",
          (UINT32)Domain
          ));
        return FALSE;
      }
    }
  }

  return TRUE;
}

STATIC
BOOLEAN
PmCpuTablesAreValid (
  IN CONST UINT8  *Buffer
  )
{
  CONST PM_CPU_DOMAIN_DATA  *Domain;
  UINT32                    EntryOffset;
  UINT32                    Frequency;
  UINT32                    Power;
  UINT32                    PreviousFrequency;
  UINT32                    PreviousVoltage;
  UINT32                    Voltage;
  UINT32                    PreviousPower;
  UINTN                     CpuDomain;
  UINTN                     Opp;

  for (CpuDomain = 0; CpuDomain < ARRAY_SIZE (mCpuDomains); CpuDomain++) {
    Domain = &mCpuDomains[CpuDomain];
    if (PmRead16 (Buffer, PM_CONFIG_OPP_DOMAIN_OFFSET (Domain->Domain)) ==
        PM_CONFIG_OPP_DOMAIN_DISABLED)
    {
      DEBUG ((DEBUG_ERROR, "[PmConfigUpdate] Invalid disabled %a OPP header\n", Domain->Name));
      return FALSE;
    }

    if ((PmRead16 (Buffer, PM_CONFIG_OPP_DOMAIN_OFFSET (Domain->Domain)) != Domain->Size) ||
        (PmRead16 (
           Buffer,
           PM_CONFIG_OPP_DOMAIN_OFFSET (Domain->Domain) + sizeof (UINT16)
           ) != Domain->SustainedIndex))
    {
      DEBUG ((DEBUG_ERROR, "[PmConfigUpdate] Unsupported %a OPP header\n", Domain->Name));
      return FALSE;
    }

    PreviousFrequency = 0;
    PreviousVoltage   = 0;
    PreviousPower     = 0;
    for (Opp = 0; Opp < PM_CONFIG_OPP_ENTRY_COUNT; Opp++) {
      EntryOffset = PM_CONFIG_OPP_ENTRY_OFFSET (Domain->Domain, Opp);
      Frequency   = PmRead32 (Buffer, EntryOffset + PM_CONFIG_OPP_LEVEL_OFFSET);
      Power       = PmRead32 (Buffer, EntryOffset + PM_CONFIG_OPP_POWER_OFFSET);
      Voltage     = PmRead32 (Buffer, EntryOffset + PM_CONFIG_OPP_VOLTAGE_OFFSET);

      if (Opp >= Domain->Size) {
        if ((Frequency != MAX_UINT32) || (Voltage != MAX_UINT32) ||
            (PmRead32 (Buffer, EntryOffset + PM_CONFIG_OPP_FREQUENCY_OFFSET) != MAX_UINT32) ||
            (PmRead32 (Buffer, EntryOffset + PM_CONFIG_OPP_POWER_OFFSET) != MAX_UINT32))
        {
          DEBUG ((DEBUG_ERROR, "[PmConfigUpdate] %a has a hidden OPP\n", Domain->Name));
          return FALSE;
        }

        continue;
      }

      if ((Frequency < PM_CONFIG_CPU_FREQUENCY_MIN) ||
          (Frequency > (((CpuDomain >= 2U) &&
                         (Buffer[PM_CONFIG_OPP_VALID_OFFSET] == PM_CONFIG_OPP_CPU_OC_LEGACY)) ?
                        2600U : PM_CONFIG_CPU_FREQUENCY_MAX)) ||
          ((Frequency % 10U) != 0) ||
          (Voltage < PM_CONFIG_CPU_VOLTAGE_MIN) ||
          (Voltage > PM_CONFIG_CPU_VOLTAGE_MAX) ||
          ((Voltage % 10U) != 0) ||
          (PmRead32 (Buffer, EntryOffset + PM_CONFIG_OPP_FREQUENCY_OFFSET) != Frequency * 1000U) ||
          (Power == 0) ||
          (Power > PM_CONFIG_CPU_POWER_MAX))
      {
        DEBUG ((DEBUG_ERROR, "[PmConfigUpdate] Invalid %a OPP %u\n", Domain->Name, (UINT32)Opp));
        return FALSE;
      }

      if ((Opp > 0) &&
          ((Frequency <= PreviousFrequency) || (Voltage < PreviousVoltage) ||
           (Power < PreviousPower)))
      {
        DEBUG ((DEBUG_ERROR, "[PmConfigUpdate] %a table is not monotonic\n", Domain->Name));
        return FALSE;
      }

      if ((Opp == Domain->ProtectedIndex) &&
          ((Frequency != PM_CONFIG_CPU_BOOT_LEVEL) ||
           (Voltage != PM_CONFIG_CPU_BOOT_VOLTAGE))) {
        return FALSE;
      }
      PreviousFrequency = Frequency;
      PreviousVoltage   = Voltage;
      PreviousPower     = Power;
    }
  }

  return TRUE;
}

BOOLEAN
PmConfigHeaderIsValid (
  IN CONST UINT8  *Buffer
  )
{
  UINT32  ExpectedCrc1;
  UINT32  ExpectedCrc2;
  UINT32  Length;

  if ((PmRead16 (Buffer, PM_CONFIG_VERSION_MAJOR_OFFSET) !=
       PM_CONFIG_SCHEMA_MAJOR) ||
      (PmRead16 (Buffer, PM_CONFIG_VERSION_MINOR_OFFSET) !=
       PM_CONFIG_SCHEMA_MINOR) ||
      (PmRead32 (Buffer, PM_CONFIG_SIGNATURE_OFFSET) != PM_CONFIG_SIGNATURE))
  {
    DEBUG ((DEBUG_ERROR, "[PmConfigUpdate] Unsupported PM config header\n"));
    return FALSE;
  }

  Length = PmRead32 (Buffer, PM_CONFIG_LENGTH_OFFSET);
  if ((Length < PM_CONFIG_SCHEMA_MIN_LENGTH) ||
      (Length > PM_CONFIG_BIN_SIZE) ||
      ((Length & (sizeof (UINT32) - 1)) != 0))
  {
    DEBUG ((DEBUG_ERROR, "[PmConfigUpdate] Invalid PM config length: %u\n", Length));
    return FALSE;
  }

  PmCalculateChecksum (Buffer, Length, &ExpectedCrc1, &ExpectedCrc2);
  if ((PmRead32 (Buffer, PM_CONFIG_CRC1_OFFSET) != ExpectedCrc1) ||
      (PmRead32 (Buffer, PM_CONFIG_CRC2_OFFSET) != ExpectedCrc2))
  {
    DEBUG ((DEBUG_ERROR, "[PmConfigUpdate] PM config checksum mismatch\n"));
    return FALSE;
  }

  return TRUE;
}

BOOLEAN
PmValidateConfig (
  IN CONST UINT8  *Buffer
  )
{
  if (!PmConfigHeaderIsValid (Buffer)) {
    return FALSE;
  }

  // Recognized foreign tables remain readable for status only.
  // Never interpret reserved bytes in an inactive external table.
  switch (Buffer[PM_CONFIG_OPP_VALID_OFFSET]) {
    case 0xFFU:
    case PM_CONFIG_OPP_EXTERNAL_INVALID:
    case 0U:
    case PM_CONFIG_OPP_EXTERNAL_PARTIAL:
      break;
    case PM_CONFIG_OPP_CPU_OC_LEGACY:
    case PM_CONFIG_OPP_CPU_OC:
      // CPU OC keeps the board-selected PMIC scheme. Foreign rail overrides
      // require explicit review instead of being silently reused for CPU OC.
      if (((PmRead32 (Buffer, PM_CONFIG_PMIC_VALID_OFFSET) & 1U) == 0U) ||
          !PmCpuTablesAreValid (Buffer) || !PmNonCpuDomainsAreUnused (Buffer)) {
        return FALSE;
      }
      break;
    default:
      return FALSE;
  }

  return TRUE;
}

BOOLEAN
PmConfigIsCpuOwned (
  IN CONST UINT8  *Buffer
  )
{
  if (!PmConfigHeaderIsValid (Buffer)) {
    return FALSE;
  }

  if ((Buffer[PM_CONFIG_OPP_VALID_OFFSET] == 0xFFU) ||
      (Buffer[PM_CONFIG_OPP_VALID_OFFSET] == PM_CONFIG_OPP_EXTERNAL_INVALID)) {
    return TRUE;
  }

  // A malformed CPU table can still be disabled, but a foreign non-CPU table
  // must never be disabled or replaced by the default Vendor request.
  return ((Buffer[PM_CONFIG_OPP_VALID_OFFSET] == PM_CONFIG_OPP_CPU_OC) ||
          (Buffer[PM_CONFIG_OPP_VALID_OFFSET] == PM_CONFIG_OPP_CPU_OC_LEGACY)) &&
         PmNonCpuDomainsAreUnused (Buffer);
}

STATIC
VOID
PmExpectedOpp (
  IN CONST RADXA_PM_TUNING_DATA  *Settings,
  IN UINTN                       CpuDomain,
  IN UINTN                       Opp,
  OUT UINT16                     *Frequency,
  OUT UINT16                     *Voltage,
  OUT UINT32                     *Power
  )
{
  CONST PM_CPU_DOMAIN_DATA  *Domain;
  UINTN                     Index;
  UINTN                     Point;
  UINT32                    Estimate;

  Domain = &mCpuDomains[CpuDomain];
  *Power = 0;
  // Keep the estimated power monotonic even when voltage scaling changes the
  // interpolation slope. These estimates are not measured overclock power.
  for (Point = 0; Point <= Opp; Point++) {
    Index = PmSettingsIndex (CpuDomain, Point);
    *Frequency = Settings->CpuFrequency[Index];
    *Voltage = Settings->CpuVoltage[Index];
    if (Point == Domain->ProtectedIndex) {
      *Frequency = PM_CONFIG_CPU_BOOT_LEVEL;
      *Voltage = PM_CONFIG_CPU_BOOT_VOLTAGE;
    }
    Estimate = PmEstimatedPower (Domain, *Frequency, *Voltage);
    if (Estimate > *Power) {
      *Power = Estimate;
    }
  }
}

BOOLEAN
PmProfileMatches (
  IN CONST UINT8                 *Buffer,
  IN CONST RADXA_PM_TUNING_DATA  *Settings
  )
{
  CONST PM_CPU_DOMAIN_DATA  *Domain;
  UINT16                    Frequency;
  UINT32                    Power;
  UINT16                    Voltage;
  UINTN                     CpuDomain;
  UINTN                     EntryOffset;
  UINTN                     Opp;

  if (!PmSettingsAreValid (Settings) || !PmValidateConfig (Buffer)) {
    return FALSE;
  }

  if (Settings->Profile == RADXA_PM_PROFILE_VENDOR) {
    return ((Buffer[PM_CONFIG_OPP_VALID_OFFSET] == PM_CONFIG_OPP_EXTERNAL_INVALID) ||
            (Buffer[PM_CONFIG_OPP_VALID_OFFSET] == 0xFFU));
  }

  if ((Buffer[PM_CONFIG_OPP_VALID_OFFSET] != PM_CONFIG_OPP_CPU_OC) ||
      !PmNonCpuDomainsAreUnused (Buffer))
  {
    return FALSE;
  }

  EntryOffset = PM_CONFIG_OPP_ENTRY_OFFSET (PM_CONFIG_LITTLE_DOMAIN, 0U);
  if (Settings->LittleMode == RADXA_PM_LITTLE_NATIVE) {
    if (PmRead16 (Buffer, PM_CONFIG_OPP_DOMAIN_OFFSET (PM_CONFIG_LITTLE_DOMAIN)) !=
        PM_CONFIG_OPP_DOMAIN_DISABLED) {
      return FALSE;
    }
  } else if ((PmRead16 (Buffer, PM_CONFIG_OPP_DOMAIN_OFFSET (PM_CONFIG_LITTLE_DOMAIN)) != 1U) ||
             (PmRead32 (Buffer, EntryOffset + PM_CONFIG_OPP_LEVEL_OFFSET) != Settings->LittleMaxFrequency) ||
             (PmRead32 (Buffer, EntryOffset + PM_CONFIG_OPP_VOLTAGE_OFFSET) != Settings->LittleMinVoltage)) {
    return FALSE;
  }

  for (CpuDomain = 0; CpuDomain < ARRAY_SIZE (mCpuDomains); CpuDomain++) {
    Domain = &mCpuDomains[CpuDomain];
    for (Opp = 0; Opp < Domain->Size; Opp++) {
      PmExpectedOpp (Settings, CpuDomain, Opp, &Frequency, &Voltage, &Power);
      EntryOffset = PM_CONFIG_OPP_ENTRY_OFFSET (Domain->Domain, Opp);
      if ((PmRead32 (Buffer, EntryOffset + PM_CONFIG_OPP_LEVEL_OFFSET) != Frequency) ||
          (PmRead32 (Buffer, EntryOffset + PM_CONFIG_OPP_VOLTAGE_OFFSET) != Voltage) ||
          (PmRead32 (Buffer, EntryOffset + PM_CONFIG_OPP_FREQUENCY_OFFSET) != Frequency * 1000U) ||
          (PmRead32 (Buffer, EntryOffset + PM_CONFIG_OPP_POWER_OFFSET) != Power))
      {
        return FALSE;
      }
    }
  }

  return TRUE;
}

BOOLEAN
PmApplyProfile (
  IN OUT UINT8                    *Buffer,
  IN CONST RADXA_PM_TUNING_DATA   *Settings
  )
{
  CONST PM_CPU_DOMAIN_DATA  *Domain;
  UINT16                    Frequency;
  UINT16                    Voltage;
  UINT32                    Crc1;
  UINT32                    Crc2;
  UINT32                    Length;
  UINT32                    Power;
  UINTN                     CpuDomain;
  UINTN                     EntryOffset;
  UINTN                     Opp;

  if (!PmSettingsAreValid (Settings) || !PmConfigIsCpuOwned (Buffer)) {
    return FALSE;
  }
  if ((Settings->Profile == RADXA_PM_PROFILE_CUSTOM) &&
      (!PmValidateConfig (Buffer) ||
       ((PmRead32 (Buffer, PM_CONFIG_PMIC_VALID_OFFSET) & 1U) == 0U))) {
    return FALSE;
  }

  if (Settings->Profile == RADXA_PM_PROFILE_VENDOR) {
    Buffer[PM_CONFIG_OPP_VALID_OFFSET] = PM_CONFIG_OPP_EXTERNAL_INVALID;
  } else {
    Buffer[PM_CONFIG_OPP_VALID_OFFSET] = PM_CONFIG_OPP_CPU_OC;
    // Populate BIG/MID tables and the optional LITTLE descriptor afresh.
    SetMem (Buffer + PM_CONFIG_OPP_DOMAIN_BASE_OFFSET,
            PM_CONFIG_OPP_DOMAIN_SIZE * PM_CONFIG_OPP_DOMAIN_COUNT, 0xFFU);
  }

  if (Settings->Profile != RADXA_PM_PROFILE_VENDOR) {
    for (CpuDomain = 0; CpuDomain < ARRAY_SIZE (mCpuDomains); CpuDomain++) {
      Domain = &mCpuDomains[CpuDomain];
      PmWrite16 (Buffer, PM_CONFIG_OPP_DOMAIN_OFFSET (Domain->Domain), Domain->Size);
      PmWrite16 (Buffer, PM_CONFIG_OPP_DOMAIN_OFFSET (Domain->Domain) + sizeof (UINT16),
                 Domain->SustainedIndex);
      for (Opp = 0; Opp < Domain->Size; Opp++) {
        PmExpectedOpp (Settings, CpuDomain, Opp, &Frequency, &Voltage, &Power);
        EntryOffset = PM_CONFIG_OPP_ENTRY_OFFSET (Domain->Domain, Opp);
        PmWrite32 (Buffer, EntryOffset + PM_CONFIG_OPP_LEVEL_OFFSET, Frequency);
        PmWrite32 (Buffer, EntryOffset + PM_CONFIG_OPP_VOLTAGE_OFFSET, Voltage);
        PmWrite32 (Buffer, EntryOffset + PM_CONFIG_OPP_FREQUENCY_OFFSET, Frequency * 1000U);
        PmWrite32 (Buffer, EntryOffset + PM_CONFIG_OPP_POWER_OFFSET, Power);
      }
    }
  }

  if ((Settings->Profile == RADXA_PM_PROFILE_CUSTOM) &&
      (Settings->LittleMode == RADXA_PM_LITTLE_CUSTOM)) {
    PmWrite16 (Buffer, PM_CONFIG_OPP_DOMAIN_OFFSET (PM_CONFIG_LITTLE_DOMAIN), 1U);
    PmWrite16 (Buffer, PM_CONFIG_OPP_DOMAIN_OFFSET (PM_CONFIG_LITTLE_DOMAIN) + 2U, 0U);
    EntryOffset = PM_CONFIG_OPP_ENTRY_OFFSET (PM_CONFIG_LITTLE_DOMAIN, 0U);
    PmWrite32 (Buffer, EntryOffset + PM_CONFIG_OPP_LEVEL_OFFSET, Settings->LittleMaxFrequency);
    PmWrite32 (Buffer, EntryOffset + PM_CONFIG_OPP_VOLTAGE_OFFSET, Settings->LittleMinVoltage);
    PmWrite32 (Buffer, EntryOffset + PM_CONFIG_OPP_FREQUENCY_OFFSET,
               (UINT32)Settings->LittleMaxFrequency * 1000U);
    PmWrite32 (Buffer, EntryOffset + PM_CONFIG_OPP_POWER_OFFSET, 0U);
  }

  Length = PmRead32 (Buffer, PM_CONFIG_LENGTH_OFFSET);
  PmCalculateChecksum (Buffer, Length, &Crc1, &Crc2);
  PmWrite32 (Buffer, PM_CONFIG_CRC1_OFFSET, Crc1);
  PmWrite32 (Buffer, PM_CONFIG_CRC2_OFFSET, Crc2);
  return TRUE;
}

UINT8
PmSavedProfile (IN CONST UINT8 *Buffer)
{
  if ((Buffer[PM_CONFIG_OPP_VALID_OFFSET] == 1U) ||
      (Buffer[PM_CONFIG_OPP_VALID_OFFSET] == 0xFFU)) {
    return RADXA_PM_PROFILE_VENDOR;
  }
  if ((Buffer[PM_CONFIG_OPP_VALID_OFFSET] == PM_CONFIG_OPP_CPU_OC) ||
      (Buffer[PM_CONFIG_OPP_VALID_OFFSET] == PM_CONFIG_OPP_CPU_OC_LEGACY)) {
    return RADXA_PM_PROFILE_CUSTOM;
  }
  return RADXA_PM_SAVED_LEGACY;
}
