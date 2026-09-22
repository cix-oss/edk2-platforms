#ifndef _RADXA_SETUP_VAR_H_
#define _RADXA_SETUP_VAR_H_

#define RADXA_SETUP_VARIABLE_GUID \
  { 0xeedf122d, 0xa912, 0x43d5, {0xaa, 0x11, 0x14, 0x27, 0x62, 0xdd, 0x96, 0xfa}}

#define RADXA_SETUP_VAR  L"RadxaSetupVar"

extern EFI_GUID  gRadxaSetupVariableGuid;

#define RADXA_SETUP_UFS_POWER_AUTO          0
#define RADXA_SETUP_UFS_POWER_ON            1
#define RADXA_SETUP_UFS_POWER_OFF           2
#define RADXA_SETUP_UFS_POWER_NOT_AVAILABLE 0xFF

#pragma pack(1)

typedef struct {
  UINT8     UFSPowerMode;
  UINT8     EnableAcpiScmi;
} RADXA_SETUP_DATA;

// CPU settings use a separate variable; never extend RADXA_SETUP_DATA.
#define RADXA_PM_PROFILE_VENDOR      0
#define RADXA_PM_PROFILE_CUSTOM      2

// Wire mode 0 stores a fixed minimum nominal request per OPP. ABI 5 may raise
// actual CPU/DSU rail voltages to satisfy coupling constraints.
#define RADXA_PM_VOLTAGE_FIXED       0
#define RADXA_PM_CPU_DOMAIN_COUNT    4
#define RADXA_PM_OPP_COUNT           13
#define RADXA_PM_CPU_VALUE_COUNT     52
#define RADXA_PM_TUNING_REVISION     2
#define RADXA_PM_TUNING_V1_SIZE      273U
#define RADXA_PM_LITTLE_NATIVE       0U
#define RADXA_PM_LITTLE_CUSTOM       2U
#define RADXA_PM_TUNING_SIGNATURE    0x31434F43U

typedef struct {
  UINT8     Profile;
  UINT16    CpuFrequency[RADXA_PM_CPU_VALUE_COUNT];
  UINT16    CpuVoltage[RADXA_PM_CPU_VALUE_COUNT];
  UINT8     Reserved[RADXA_PM_CPU_DOMAIN_COUNT];
  UINT8     CpuVoltageMode[RADXA_PM_CPU_VALUE_COUNT];
  UINT16    Revision;
  UINT16    DataSize;
  UINT32    Signature;
  // Keep the complete revision-1 prefix at its original offsets.
  UINT8     LittleMode;
  UINT16    LittleMaxFrequency;
  UINT16    LittleMinVoltage;
} RADXA_PM_TUNING_DATA;

#define RADXA_PM_TUNING_VAR L"RadxaCpuOcVar"
#define RADXA_PM_STATUS_VAR L"RadxaCpuOcStatusVar"
#define RADXA_PM_SAVED_LEGACY 3U
#define RADXA_PM_SAVED_UNKNOWN 255U
#define RADXA_PM_ERROR_NONE 0U
#define RADXA_PM_ERROR_UNSUPPORTED 1U
#define RADXA_PM_ERROR_SETTINGS 2U
#define RADXA_PM_ERROR_CONFIG 3U
#define RADXA_PM_ERROR_READ 4U
#define RADXA_PM_ERROR_WRITE 5U
#define RADXA_PM_ERROR_VERIFY 6U
#define RADXA_PM_ERROR_MEMORY 7U
#define RADXA_PM_ERROR_VARIABLE 8U
#define RADXA_PM_ERROR_FOREIGN 9U
#define RADXA_PM_STATE_READY 0U
#define RADXA_PM_STATE_RESET_PENDING 1U
#define RADXA_PM_STATE_FAILED 2U

#define RADXA_PM_RUNTIME_UNKNOWN 0U
#define RADXA_PM_RUNTIME_INACTIVE 1U
#define RADXA_PM_RUNTIME_ACCEPTED 2U
#define RADXA_PM_RUNTIME_REJECTED 3U
#define RADXA_PM_RUNTIME_DIFFERENT_CONFIG 4U
#define RADXA_PM_REJECT_COUPLED_RANGE 8U

typedef struct {
  UINT8 Revision;
  UINT8 CustomSupported;
  UINT8 SavedProfile;
  UINT8 State;
  UINT8 LastError;
  UINT8 RuntimeState;
  UINT8 RuntimeReason;
} RADXA_PM_STATUS_DATA;

#pragma pack()

#endif
