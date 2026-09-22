"""Fault-inject the actual bounded raw-entry C functions on the host."""
# SPDX-License-Identifier: BSD-2-Clause-Patent
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from test_policy import ROOT, UEFI

FLASH = ROOT / "Platform/CIX/Sky1/Drivers/FirmwareUpdateDxe"


def function(source, name, result):
    start = re.search(r"^" + re.escape(name) + r" \(", source, re.M).start()
    body = source.index("{", start)
    depth, end = 1, body + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return result + "\n" + source[start:end] + "\n"


PREAMBLE = r'''
#include <Uefi.h>
#define EFIAPI
#define SIZE_4KB 4096U
#define SIZE_1MB (1024U*1024U)
#define BIT31 (1U<<31)
#define EFI_ERROR(x) (((x) >> 63) != 0)
#define EFI_SUCCESS 0
#define EFI_DEVICE_ERROR (UINT64_C(1)<<63)
typedef UINT64 EFI_STATUS;
#include <Protocol/CixFwUpdateProtocol.h>
static UINT8 flash[16U*1024U*1024U];
static unsigned reads, writes, allocation_calls, allocations;
static unsigned fail_read_at, fail_alloc_at;
static int write_fail, corrupt;
typedef struct DISK DISK;
struct DISK { EFI_STATUS (*WriteDisk)(DISK*,UINT32,UINT64,UINTN,VOID*); };
typedef struct { UINTN Size; } NOR_FLASH_INSTANCE;
static NOR_FLASH_INSTANCE instance={sizeof(flash)};
#define INSTANCE_FROM_DISKIO_THIS(x) (&instance)
static EFI_STATUS write_disk(DISK *d,UINT32 m,UINT64 off,UINTN size,VOID *b) {
  (void)d; (void)m; writes++;
  assert(b && off<=instance.Size && size<=instance.Size-off);
  if (write_fail) return EFI_DEVICE_ERROR;
  memcpy(flash+off,b,size); if (corrupt) flash[off]^=1;
  return EFI_SUCCESS;
}
static DISK disk={write_disk};
static DISK *NorFlashDiskIo=&disk;
static UINT32 MediaId;
/* Source type declarations are inserted here. */
static FIRMWARE_PROGRAM_STATUS FwProgStatus;
static VOID *AllocatePool(UINTN size) {
  allocation_calls++;
  if (allocation_calls==fail_alloc_at) return NULL;
  VOID *p=malloc(size); if (p) allocations++; return p;
}
static VOID *AllocateZeroPool(UINTN size) {
  VOID *p=AllocatePool(size); if (p) memset(p,0,size); return p;
}
static VOID FreePool(VOID *p) { if (p) { assert(allocations); allocations--; } free(p); }
static EFI_STATUS LocateNorFlashDiskIoProtocol(void) {
  return NorFlashDiskIo ? EFI_SUCCESS : EFI_DEVICE_ERROR;
}
static EFI_STATUS CixFlashReadWrapper(UINT32 off,UINTN size,VOID *b) {
  reads++;
  assert(b && off<=instance.Size && size<=instance.Size-off);
  if (reads==fail_read_at) return EFI_DEVICE_ERROR;
  memcpy(b,flash+off,size); return EFI_SUCCESS;
}
'''

MAIN = r'''
static FIRMWARE_HEADER *header(void) { return (FIRMWARE_HEADER *)(flash+FIRMWARE_HEADER_OFFSET); }
static void fixture(void) {
  assert(allocations==0);
  memset(flash,0,sizeof(flash));
  reads=writes=allocation_calls=fail_read_at=fail_alloc_at=0;
  write_fail=corrupt=0; NorFlashDiskIo=&disk; instance.Size=sizeof(flash);
  FIRMWARE_HEADER *h=header(); h->Signature=FIRMWARE_HEADER_SIGNATURE; h->EntryCount=2;
  h->EntryNode[0]=(FIRMWARE_ENTRY){FIRMWARE_TYPE_BootLoader_1,0,SIZE_1MB,0};
  h->EntryNode[1]=(FIRMWARE_ENTRY){FIRMWARE_TYPE_PM_CONF,0x301000,SIZE_4KB,0};
}
static UINT16 update(UINT8 *b, ENTRY_UPDATE_METHOD method) {
  UINT16 result=CixFirmwareRawEntryUpdate(FIRMWARE_TYPE_PM_CONF,b,SIZE_4KB,method,NULL);
  assert((result>>8)==FIRMWARE_TYPE_PM_CONF && allocations==0);
  return result & 255;
}
int main(void) {
  UINT8 b[SIZE_4KB]; UINT8 *bl1=malloc(SIZE_1MB); assert(bl1); memset(b,0xa5,sizeof(b));
  fixture();
  assert(update(b,ENTRY_WRITE)==FIRMWARE_RET_SUCCESS && writes==1 && reads==3);
  assert(memcmp(b,flash+0x301000,sizeof(b))==0);
  assert(update(b,ENTRY_WRITE)==FIRMWARE_RET_SUCCESS && writes==1);
  memset(b,0,sizeof(b));
  assert(update(b,ENTRY_READ)==FIRMWARE_RET_SUCCESS && b[0]==0xa5 && writes==1);

  /* Each allocation failure must precede the first write. */
  for (unsigned fail=1; fail<=2; fail++) {
    fixture(); fail_alloc_at=fail;
    assert(update(b,ENTRY_WRITE)==FIRMWARE_RET_OUT_OF_RESOURCE && writes==0);
  }
  for (unsigned fail=1; fail<=3; fail++) {
    fixture(); fail_alloc_at=fail;
    assert(update(NULL,ENTRY_ERASE)==FIRMWARE_RET_OUT_OF_RESOURCE && writes==0);
  }
  /* Distinguish directory, pre-write, and post-write verification reads. */
  for (unsigned fail=1; fail<=2; fail++) {
    fixture(); fail_read_at=fail;
    assert(update(b,ENTRY_WRITE)==FIRMWARE_RET_READ_ERR && writes==0);
  }
  fixture(); fail_read_at=3;
  assert(update(b,ENTRY_WRITE)==FIRMWARE_RET_ERR_VERIFY && writes==1);
  fixture(); write_fail=1;
  assert(update(b,ENTRY_WRITE)==FIRMWARE_RET_ERR_PROG && writes==1);
  fixture(); corrupt=1;
  assert(update(b,ENTRY_WRITE)==FIRMWARE_RET_ERR_VERIFY && writes==1);
  fixture(); fail_read_at=2;
  assert(update(b,ENTRY_READ)==FIRMWARE_RET_READ_ERR && writes==0);
  fixture(); NorFlashDiskIo=NULL;
  assert(update(b,ENTRY_WRITE)==FIRMWARE_RET_NO_FLASH_PROG_PROTOCOL && writes==0);

  /* BL1 reads must cover the exact declared entry, and never permit writes. */
  fixture();
  assert(CixFirmwareRawEntryUpdate(1,bl1,SIZE_1MB,ENTRY_READ,NULL)==0x100);
  assert((CixFirmwareRawEntryUpdate(1,b,SIZE_4KB,ENTRY_READ,NULL)&255)==FIRMWARE_RET_ERR_SIZE);
  assert((CixFirmwareRawEntryUpdate(1,bl1,SIZE_1MB,ENTRY_WRITE,NULL)&255)==FIRMWARE_RET_ERR_INPUT);
  assert((CixFirmwareRawEntryUpdate(1,NULL,SIZE_1MB,ENTRY_ERASE,NULL)&255)==FIRMWARE_RET_ERR_INPUT);
  assert((CixFirmwareRawEntryUpdate(1,bl1,SIZE_1MB+1,ENTRY_READ,NULL)&255)==FIRMWARE_RET_ERR_INPUT);
  assert((CixFirmwareRawEntryUpdate(1,bl1,0,ENTRY_READ,NULL)&255)==FIRMWARE_RET_ERR_INPUT);
  assert((CixFirmwareRawEntryUpdate(4,b,SIZE_4KB-1,ENTRY_WRITE,NULL)&255)==FIRMWARE_RET_ERR_INPUT);
  assert(update(NULL,ENTRY_READ)==FIRMWARE_RET_ERR_INPUT);
  assert(update(NULL,ENTRY_WRITE)==FIRMWARE_RET_ERR_INPUT);
  assert(update(b,(ENTRY_UPDATE_METHOD)9)==FIRMWARE_RET_ERR_INPUT);
  assert((CixFirmwareRawEntryUpdate(2,b,SIZE_4KB,ENTRY_READ,NULL)&255)==FIRMWARE_RET_ERR_INPUT);
  assert(writes==0 && allocations==0);

  /* Parse the actual flash directory rather than a mocked successful lookup. */
  fixture(); header()->EntryCount=0;
  assert(update(b,ENTRY_WRITE)==FIRMWARE_RET_ERR_HEADER && writes==0);
  fixture(); header()->EntryCount=11;
  assert(update(b,ENTRY_WRITE)==FIRMWARE_RET_ERR_HEADER && writes==0);
  fixture(); header()->EntryCount=UINT32_MAX;
  assert(update(b,ENTRY_WRITE)==FIRMWARE_RET_ERR_HEADER && writes==0);
  fixture(); header()->Signature=0;
  assert(update(b,ENTRY_WRITE)==FIRMWARE_RET_ERR_HEADER && writes==0);
  fixture(); header()->ControlFlag=UPDATE_OTA_PACKAGE;
  assert(update(b,ENTRY_WRITE)==FIRMWARE_RET_ERR_HEADER && writes==0);
  fixture(); header()->EntryNode[1].Base=0x301001;
  assert(update(b,ENTRY_WRITE)==FIRMWARE_RET_ERR_4KB_ALIGN && writes==0);
  fixture(); header()->EntryNode[1].Base=FIRMWARE_HEADER_OFFSET;
  assert(update(b,ENTRY_WRITE)==FIRMWARE_RET_ERR_LAYOUT && writes==0);
  fixture(); header()->EntryNode[1].Base=0x80000;
  assert(update(b,ENTRY_WRITE)==FIRMWARE_RET_ERR_LAYOUT && writes==0);
  fixture(); header()->EntryNode[1].Base=sizeof(flash);
  assert(update(b,ENTRY_WRITE)==FIRMWARE_RET_ERR_LAYOUT && writes==0);
  fixture(); header()->EntryNode[1].Base=0xfffff000;
  assert(update(b,ENTRY_WRITE)==FIRMWARE_RET_ERR_LAYOUT && writes==0);
  fixture(); header()->EntryNode[1].Length=0;
  assert(update(b,ENTRY_WRITE)==FIRMWARE_RET_ERR_LAYOUT && writes==0);
  fixture(); header()->EntryNode[1].Length=UINT32_MAX;
  assert(update(b,ENTRY_WRITE)==FIRMWARE_RET_ERR_LAYOUT && writes==0);
  fixture(); header()->EntryNode[1].Length=8192;
  assert(update(b,ENTRY_WRITE)==FIRMWARE_RET_ERR_SIZE && writes==0);
  fixture(); header()->EntryCount=3;
  header()->EntryNode[2]=(FIRMWARE_ENTRY){FIRMWARE_TYPE_PM_CONF,0x401000,SIZE_4KB,0};
  assert(update(b,ENTRY_WRITE)==FIRMWARE_RET_ERR_LAYOUT && writes==0);
  fixture(); header()->EntryCount=1;
  assert(update(b,ENTRY_WRITE)==FIRMWARE_RET_TYPE_NOT_FOUND && writes==0);
  fixture(); instance.Size=FIRMWARE_HEADER_OFFSET;
  assert(update(b,ENTRY_WRITE)==FIRMWARE_RET_ERR_LAYOUT && writes==0);

  /* Legacy config entries record occupied bytes within a reserved sector. */
  const UINT8 legacy_types[]={FIRMWARE_TYPE_MEM_CONF,FIRMWARE_TYPE_TFA_CONF,FIRMWARE_TYPE_SECURE_DEBUG};
  memset(b,0xa5,sizeof(b));
  for (unsigned i=0;i<ARRAY_SIZE(legacy_types);i++) {
    UINT8 type=legacy_types[i]; UINT16 prefix=(UINT16)type<<8;
    fixture(); header()->EntryNode[1].Type=type; header()->EntryNode[1].Length=1488;
    assert(CixFirmwareRawEntryUpdate(type,b,SIZE_4KB,ENTRY_WRITE,NULL)==prefix && writes==1);
    assert(memcmp(flash+0x301000,b,SIZE_4KB)==0);
    assert(CixFirmwareRawEntryUpdate(type,b,SIZE_4KB,ENTRY_READ,NULL)==prefix);
    assert(CixFirmwareRawEntryUpdate(type,NULL,SIZE_4KB,ENTRY_ERASE,NULL)==prefix && writes==2);
    for (unsigned j=0;j<SIZE_4KB;j++) assert(flash[0x301000+j]==255);
    assert(allocations==0);
  }
  fixture(); header()->EntryNode[1].Type=FIRMWARE_TYPE_MEM_CONF; header()->EntryNode[1].Length=16384;
  memset(flash+0x302000,0x3c,16384-SIZE_4KB);
  assert(CixFirmwareRawEntryUpdate(FIRMWARE_TYPE_MEM_CONF,b,SIZE_4KB,ENTRY_WRITE,NULL)==0x300 && writes==1);
  assert(memcmp(flash+0x301000,b,SIZE_4KB)==0);
  for (unsigned i=0;i<16384-SIZE_4KB;i++) assert(flash[0x302000+i]==0x3c);
  assert(allocations==0);
  fixture(); header()->EntryNode[1].Length=1488;
  assert(update(b,ENTRY_WRITE)==FIRMWARE_RET_ERR_SIZE && writes==0);
  fixture(); header()->EntryNode[1].Type=FIRMWARE_TYPE_MEM_CONF; header()->EntryNode[1].Length=1488;
  header()->EntryCount=3;
  header()->EntryNode[2]=(FIRMWARE_ENTRY){FIRMWARE_TYPE_TFA_CONF,0x301800,512,0};
  assert((CixFirmwareRawEntryUpdate(FIRMWARE_TYPE_MEM_CONF,b,SIZE_4KB,ENTRY_WRITE,NULL)&255)==FIRMWARE_RET_ERR_LAYOUT && writes==0);
  fixture(); header()->EntryNode[1].Type=FIRMWARE_TYPE_MEM_CONF; header()->EntryNode[1].Length=1488;
  instance.Size=0x301000+1488;
  assert((CixFirmwareRawEntryUpdate(FIRMWARE_TYPE_MEM_CONF,b,SIZE_4KB,ENTRY_WRITE,NULL)&255)==FIRMWARE_RET_ERR_LAYOUT && writes==0);
  assert(allocations==0);

  /* The alternative header follows the same bounds and read-error rules. */
  fixture(); memcpy(flash+FIRMWARE_HEADER_OFFSET_ALT,header(),SIZE_4KB); header()->Signature=0;
  assert(update(b,ENTRY_READ)==FIRMWARE_RET_SUCCESS && reads==3 && writes==0);
  fixture(); header()->Signature=0; fail_read_at=2;
  assert(update(b,ENTRY_WRITE)==FIRMWARE_RET_READ_ERR && writes==0);
  fixture(); header()->Signature=0; instance.Size=FIRMWARE_HEADER_OFFSET_ALT;
  assert(update(b,ENTRY_WRITE)==FIRMWARE_RET_ERR_LAYOUT && writes==0);
  fixture(); memcpy(flash+FIRMWARE_HEADER_OFFSET_ALT,header(),SIZE_4KB); header()->Signature=0;
  ((FIRMWARE_HEADER *)(flash+FIRMWARE_HEADER_OFFSET_ALT))->EntryNode[1].Base=FIRMWARE_HEADER_OFFSET_ALT;
  assert(update(b,ENTRY_WRITE)==FIRMWARE_RET_ERR_LAYOUT && writes==0);
  fixture(); assert(update(NULL,ENTRY_ERASE)==FIRMWARE_RET_SUCCESS && writes==1);
  for (unsigned i=0;i<SIZE_4KB;i++) assert(flash[0x301000+i]==255);
  assert(update(NULL,ENTRY_ERASE)==FIRMWARE_RET_SUCCESS && writes==1);
  assert(allocations==0); free(bl1); return 0;
}
'''


@unittest.skipUnless(os.environ.get("CIX_RUN_HOST_C_TESTS") == "1",
                     "host C compilation requires explicit opt-in")
class FlashTests(unittest.TestCase):
    def test_actual_raw_flash_functions(self):
        source = (FLASH / "FwUpdateProtocolDxe.c").read_text()
        header = (FLASH / "FirmwareUpdate.h").read_text()
        names = ("FIRMWARE_ENTRY", "FIRMWARE_HEADER", "FIRMWARE_PROGRAM_STATUS",
                 "CIX_FIRMWARE_ENTRY_INFO", "CIX_FWUP_PRIVATE_DATA")
        types = "\n".join(re.findall(
            r"typedef struct \{[^}]*\} (?:" + "|".join(names) + r");", header))
        wanted = {"UPDATE_OTA_PACKAGE", "FIRMWARE_HEADER_OFFSET",
                  "FIRMWARE_HEADER_OFFSET_ALT", "FIRMWARE_HEADER_SIGNATURE"}
        defines = "\n".join(line for line in header.splitlines()
                            if line.startswith("#define ") and line.split()[1] in wanted)
        code = PREAMBLE.replace("/* Source type declarations are inserted here. */",
                                types + "\n" + defines)
        for name, result in (("CixFindRawFirmwareEntry", "STATIC UINT16"),
                             ("CixFirmwareRawEntryUpdate", "UINT16")):
            code += function(source, name, result)
        with tempfile.TemporaryDirectory(prefix="cix-flash-test-") as tmp:
            path = Path(tmp)
            (path / "Protocol").mkdir()
            (path / "Uefi.h").write_text(UEFI)
            (path / "Protocol/FirmwareManagement.h").write_text(
                "typedef void *EFI_FIRMWARE_MANAGEMENT_UPDATE_IMAGE_PROGRESS;\n")
            (path / "test.c").write_text(code + MAIN)
            subprocess.run([
                os.environ.get("CC", "cc"), "-std=c11", "-Wall", "-Wextra", "-Werror",
                "-Wno-unused-parameter", "-fsanitize=undefined", "-fno-sanitize-recover=all",
                f"-I{path}", f"-I{ROOT}/Platform/CIX/Sky1/Include", str(path / "test.c"),
                "-o", str(path / "test")], check=True)
            subprocess.run([str(path / "test")], check=True)


if __name__ == "__main__":
    unittest.main()
