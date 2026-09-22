"""Compile and execute the actual policy C only when explicitly requested."""
# SPDX-License-Identifier: BSD-2-Clause-Patent
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
DRIVER = HERE.parent
ROOT = next(parent for parent in DRIVER.parents
            if (parent / "Platform/Radxa").is_dir())
UEFI = r'''
#ifndef TEST_UEFI_H
#define TEST_UEFI_H
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <assert.h>
typedef uint8_t UINT8; typedef uint16_t UINT16; typedef uint32_t UINT32;
typedef uint64_t UINT64; typedef size_t UINTN; typedef char CHAR8;
typedef unsigned char BOOLEAN; typedef void VOID;
typedef struct { UINT32 a; UINT16 b,c; UINT8 d[8]; } EFI_GUID;
#define IN
#define OUT
#define CONST const
#define STATIC static
#define STATIC_ASSERT _Static_assert
#define OFFSET_OF offsetof
#define MAX_UINT32 UINT32_MAX
#define TRUE 1
#define FALSE 0
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define SIGNATURE_32(a,b,c,d) ((UINT32)(a)|((UINT32)(b)<<8)|((UINT32)(c)<<16)|((UINT32)(d)<<24))
#define CopyMem memcpy
#define CompareMem memcmp
#define ZeroMem(a,b) memset((a),0,(b))
#define SetMem(a,b,c) memset((a),(c),(b))
#define DEBUG(a) ((void)0)
static inline UINT64 MultU64x32(UINT64 a, UINT32 b) { return a*b; }
static inline UINT64 DivU64x64Remainder(UINT64 a, UINT64 b, UINT64 *r) { *r=a%b; return a/b; }
#endif
'''

POLICY_TEST = r'''
#include "PmConfigPolicy.c"
static void checksum(UINT8 *b) {
  UINT32 x=0,y=0,w,n; memcpy(&n,b+8,4);
  memset(b+16,0,8);
  for (UINT32 i=0;i<n;i+=4) { memcpy(&w,b+i,4); x+=w; y+=x; }
  memcpy(b+16,&x,4); memcpy(b+20,&y,4);
}
static void fixture(UINT8 *b) {
  memset(b,0xff,4096); UINT16 major=3,minor=0; UINT32 n=4096,sig=0x46434d50;
  memcpy(b,&major,2);memcpy(b+2,&minor,2);memcpy(b+8,&n,4);memcpy(b+12,&sig,4);
  checksum(b);
}
static void invalid_word(UINT8 *b, UINTN off, UINT32 value) {
  UINT8 c[4096]; memcpy(c,b,4096);memcpy(c+off,&value,4);checksum(c);assert(!PmValidateConfig(c));
}
int main(void) {
  UINT8 b[4096],original[4096],custom[4096]; RADXA_PM_TUNING_DATA s,t;
  assert(sizeof(s)==278); assert(offsetof(RADXA_PM_TUNING_DATA,LittleMode)==273); fixture(b); memcpy(original,b,4096);
  assert(PmValidateConfig(b)); PmInitializeSettings(&s); assert(PmSettingsAreValid(&s));
  assert(PmProfileMatches(b,&s));
  // A foreign full/partial table must remain byte-identical in Vendor and Custom.
  for (unsigned flag=0; flag<=0x80; flag+=0x80) {
    memcpy(b,original,4096); b[152]=(UINT8)flag; checksum(b); assert(PmValidateConfig(b));
    memcpy(custom,b,4096);assert(!PmConfigIsCpuOwned(b));
    assert(!PmApplyProfile(b,&s));assert(!memcmp(b,custom,4096));
    t=s;t.Profile=2;assert(!PmApplyProfile(b,&t));assert(!memcmp(b,custom,4096));
  }
  memcpy(b,original,4096);s.Profile=2; assert(PmSettingsAreValid(&s));
  PmApplyProfile(b,&s);assert(PmValidateConfig(b));assert(PmProfileMatches(b,&s));memcpy(custom,b,4096);
  assert(b[152]==0xc1);
  for (unsigned d=0;d<13;d++) {
    unsigned off=153+d*212;
    if (d<3||d>6) { for(unsigned j=0;j<212;j++) assert(b[off+j]==255); continue; }
    UINT16 count,sustained;memcpy(&count,b+off,2);memcpy(&sustained,b+off+2,2);
    assert(count==(d==6?6:7));assert(sustained==2);
    for(unsigned j=4+count*16;j<212;j++) assert(b[off+j]==255);
    UINT32 level,freq,volt;
    memcpy(&level,b+off+4+2*16,4);memcpy(&volt,b+off+4+2*16+4,4);
    memcpy(&freq,b+off+4+2*16+8,4);assert(level==1500&&volt==790&&freq==1500000);
  }
  for(unsigned i=0;i<4096;i++) if (!(i>=16&&i<24)&&!(i>=152&&i<153+13*212)) assert(b[i]==original[i]);
  t=s;t.CpuFrequency[6]=3200;t.CpuFrequency[32]=3200;assert(PmSettingsAreValid(&t));
  PmApplyProfile(b,&t);assert(PmValidateConfig(b));
  t=s;t.CpuFrequency[6]=3210;assert(!PmSettingsAreValid(&t));
  t=s;t.CpuFrequency[32]=3210;assert(!PmSettingsAreValid(&t));
  t=s;t.CpuFrequency[6]=2400;assert(!PmSettingsAreValid(&t));
  t=s;t.CpuFrequency[6]=2501;assert(!PmSettingsAreValid(&t));
  // The minimum nominal 1250 mV limit applies to every CPU domain and survives encoding.
  const unsigned last_opp[]={6,19,32,44};
  for(unsigned d=0;d<4;d++) {
    for(unsigned mv=950;mv<=1250;mv+=10) {
      t=s;t.CpuVoltage[last_opp[d]]=(UINT16)mv;assert(PmSettingsAreValid(&t));
      memcpy(b,custom,4096);assert(PmApplyProfile(b,&t));
      assert(PmValidateConfig(b)&&PmProfileMatches(b,&t));
      UINT32 encoded_mv;unsigned entry=153+(d+3)*212+4+(last_opp[d]%13)*16;
      memcpy(&encoded_mv,b+entry+4,4);assert(encoded_mv==mv);
    }
    t=s;t.CpuVoltage[last_opp[d]]=1260;assert(!PmSettingsAreValid(&t));
    t=s;t.CpuVoltage[last_opp[d]]=1245;assert(!PmSettingsAreValid(&t));
    t=s;t.CpuVoltage[last_opp[d]-1]=1250;t.CpuVoltage[last_opp[d]]=1240;
    assert(!PmSettingsAreValid(&t));
  }
  // The voltage-square numerator exceeds UINT32 at 1250 mV; it must stay UINT64.
  const UINT32 maximum_power[]={15692,13846,12231,10850};
  for(unsigned d=0;d<4;d++) {
    UINT32 maximum=0;
    for(unsigned mhz=800;mhz<=3200;mhz+=10) {
      for(unsigned mv=550;mv<=1250;mv+=10) {
        UINT32 power=PmEstimatedPower(&mCpuDomains[d],(UINT16)mhz,(UINT16)mv);
        assert(power>0&&power<=PM_CONFIG_CPU_POWER_MAX);
        if(power>maximum)maximum=power;
      }
    }
    assert(maximum==maximum_power[d]);
    t=s;t.CpuVoltage[last_opp[d]]=1250;t.CpuFrequency[last_opp[d]]=3200;
    assert(PmSettingsAreValid(&t));memcpy(b,custom,4096);assert(PmApplyProfile(b,&t));
    assert(PmValidateConfig(b)&&PmProfileMatches(b,&t));
    UINT32 encoded_power;unsigned entry=153+(d+3)*212+4+(last_opp[d]%13)*16;
    memcpy(&encoded_power,b+entry+12,4);assert(encoded_power==maximum_power[d]);
  }
  // Exact revision-1 migration preserves the complete BIG/MID settings prefix.
  t=s;t.Revision=1;t.DataSize=273;t.CpuFrequency[32]=2600;
  RADXA_PM_TUNING_DATA old=t;
  assert(PmMigrateLegacySettings(&t)&&PmSettingsAreValid(&t));
  assert(t.Revision==2&&t.DataSize==278&&t.LittleMode==0&&t.LittleMaxFrequency==1800&&t.LittleMinVoltage==0);
  assert(!memcmp(&t,&old,offsetof(RADXA_PM_TUNING_DATA,Revision)));
  for(unsigned bad=0;bad<7;bad++) {
    t=old;
    switch(bad) {
    case 0:t.Revision=2;break;case 1:t.DataSize=274;break;
    case 2:t.Signature=0;break;case 3:t.Profile=1;break;
    case 4:t.CpuFrequency[32]=2610;break;case 5:t.Reserved[0]=1;break;
    default:t.CpuVoltage[2]=800;break;
    }
    RADXA_PM_TUNING_DATA before=t;
    assert(!PmMigrateLegacySettings(&t)&&!memcmp(&t,&before,sizeof(t)));
  }
  // Revision-1 Vendor did not validate inactive arrays. Preserve that contract.
  t=old;t.Profile=0;t.CpuFrequency[32]=65535;t.CpuFrequency[6]=800;
  t.CpuVoltage[2]=0;t.CpuVoltageMode[0]=255;t.Reserved[1]=255;
  RADXA_PM_TUNING_DATA inactive=t;
  assert(PmMigrateLegacySettings(&t)&&PmSettingsAreValid(&t));
  assert(!memcmp(&t,&inactive,offsetof(RADXA_PM_TUNING_DATA,Revision)));
  t.Profile=2;assert(!PmSettingsAreValid(&t));
  // C0 remains readable, but cannot match a newly emitted C1 request.
  memcpy(b,custom,4096);b[152]=0xc0;checksum(b);
  assert(PmValidateConfig(b)&&PmConfigIsCpuOwned(b)&&PmSavedProfile(b)==2);
  assert(!PmProfileMatches(b,&s));assert(PmApplyProfile(b,&s)&&b[152]==0xc1);
  t=s;t.CpuFrequency[32]=3200;assert(PmApplyProfile(b,&t));
  assert(PmValidateConfig(b));b[152]=0xc0;checksum(b);assert(!PmValidateConfig(b));

  // LITTLE is an optional scalar descriptor, not a replacement native table.
  unsigned little=153+2*212,le=little+4;
  for(unsigned mhz=1800;mhz<=2400;mhz+=10) {
    for(unsigned v=0;v<=41;v++) {
      unsigned mv=v?540+v*10:0;
      t=s;t.LittleMode=2;t.LittleMaxFrequency=(UINT16)mhz;t.LittleMinVoltage=(UINT16)mv;
      assert(PmSettingsAreValid(&t));memcpy(b,custom,4096);assert(PmApplyProfile(b,&t));
      assert(PmValidateConfig(b)&&PmConfigIsCpuOwned(b)&&PmProfileMatches(b,&t));
      assert(PmRead16(b,little)==1&&PmRead16(b,little+2)==0);
      assert(PmRead32(b,le)==mhz&&PmRead32(b,le+4)==mv&&PmRead32(b,le+8)==mhz*1000&&PmRead32(b,le+12)==0);
      for(unsigned off=le+16;off<little+212;off++)assert(b[off]==255);
      assert(!PmProfileMatches(b,&s));assert(PmApplyProfile(b,&s));
      for(unsigned off=little;off<little+212;off++)assert(b[off]==255);
    }
  }
  for(unsigned mode=0;mode<4;mode++) {
    t=s;t.LittleMode=(UINT8)mode;assert(PmSettingsAreValid(&t)==(mode==0||mode==2));
  }
  const UINT16 bad_freq[]={0,1790,1801,2410,65535};
  const UINT16 bad_volt[]={1,540,551,960,65535};
  for(unsigned i=0;i<ARRAY_SIZE(bad_freq);i++) {
    t=s;t.LittleMaxFrequency=bad_freq[i];assert(!PmSettingsAreValid(&t));
    t=s;t.LittleMinVoltage=bad_volt[i];assert(!PmSettingsAreValid(&t));
  }
  t=s;t.LittleMode=2;t.LittleMaxFrequency=2400;t.LittleMinVoltage=950;
  memcpy(b,custom,4096);assert(PmApplyProfile(b,&t));
  invalid_word(b,little,2);invalid_word(b,little,1|(1<<16));
  invalid_word(b,le,1790);invalid_word(b,le,2410);invalid_word(b,le,1801);
  invalid_word(b,le+4,540);invalid_word(b,le+4,951);invalid_word(b,le+4,960);
  invalid_word(b,le+8,2400001);invalid_word(b,le+12,1);invalid_word(b,le+16,0);
  // Legacy C0 cannot claim any LITTLE descriptor; foreign data is preserved.
  b[152]=0xc0;checksum(b);assert(!PmValidateConfig(b)&&!PmConfigIsCpuOwned(b));
  memcpy(original,b,4096);assert(!PmApplyProfile(b,&s)&&!memcmp(b,original,4096));
  b[152]=0xc1;checksum(b);assert(PmValidateConfig(b));
  memcpy(original,b,4096);t=s;t.Profile=0;
  assert(PmApplyProfile(b,&t)&&PmValidateConfig(b)&&b[152]==1);
  for(unsigned off=0;off<4096;off++)
    if(!(off>=16&&off<24)&&off!=152)assert(b[off]==original[off]);
  memcpy(b,original,4096);
  // A malformed LITTLE descriptor is not permission to replace a foreign table.
  UINT32 bad_little=2;memcpy(b+little,&bad_little,4);checksum(b);
  memcpy(original,b,4096);t=s;t.Profile=0;
  assert(!PmConfigIsCpuOwned(b)&&!PmApplyProfile(b,&t)&&!memcmp(b,original,4096));
  t=s;t.CpuVoltageMode[6]=1;assert(!PmSettingsAreValid(&t));
  t=s;t.CpuVoltage[2]=800;assert(!PmSettingsAreValid(&t));
  t=s;t.CpuFrequency[2]=1490;assert(!PmSettingsAreValid(&t));
  t=s;t.CpuVoltage[5]=780;assert(!PmSettingsAreValid(&t));
  t=s;t.Revision++;assert(!PmSettingsAreValid(&t));
  t=s;t.Reserved[0]=1;assert(!PmSettingsAreValid(&t));
  t=s;t.CpuFrequency[7]=800;assert(!PmSettingsAreValid(&t));
  t=s;t.CpuVoltageMode[51]=1;assert(!PmSettingsAreValid(&t));
  memcpy(b,custom,4096);unsigned e=153+3*212+4;
  invalid_word(b,e+8,0);invalid_word(b,e+4,0x10002ee);invalid_word(b,e+4,751);
  invalid_word(b,e+6*16+4,1260);invalid_word(b,e+6*16+4,1245);
  invalid_word(b,e+16+12,1);invalid_word(b,e+2*16+4,800);
  invalid_word(b,153+3*212,7|(3<<16));invalid_word(b,153+3*212,0xff07|(2<<16));
  invalid_word(b,153+3*212+4+7*16,0);invalid_word(b,153,0);invalid_word(b,24,0);
  // A bad custom table with an intact header can be disabled by Vendor.
  memcpy(b,custom,4096);memset(b+153+3*212+4+7*16,0,16);checksum(b);
  assert(PmConfigHeaderIsValid(b)&&!PmValidateConfig(b));
  t=s;t.Profile=0;PmApplyProfile(b,&t);assert(PmValidateConfig(b)&&b[152]==1);
  memcpy(b,custom,4096);
  // Even a CPU OC flag cannot claim ownership over a non-CPU override.
  memset(b+153,0,4);checksum(b);memcpy(original,b,4096);
  assert(!PmConfigIsCpuOwned(b));assert(!PmApplyProfile(b,&t));assert(!memcmp(b,original,4096));
  memcpy(b,custom,4096);
  // Native board PMIC overrides are never erased or silently used for Custom.
  b[152]=1;memset(b+24,0,4);checksum(b);memcpy(original,b,4096);
  assert(!PmApplyProfile(b,&s));assert(!memcmp(b,original,4096));
  assert(PmApplyProfile(b,&t));assert(!memcmp(b+24,original+24,4));
  memcpy(b,custom,4096);
  invalid_word(b,8,4097);invalid_word(b,8,3336);b[4095]^=1;assert(!PmValidateConfig(b));
  return 0;
}
'''

@unittest.skipUnless(os.environ.get("CIX_RUN_HOST_C_TESTS") == "1",
                     "host C compilation requires explicit opt-in")
class PolicyTests(unittest.TestCase):
    def test_actual_c_policy(self):
        with tempfile.TemporaryDirectory(prefix="cix-cpu-policy-") as tmp:
            p=Path(tmp);(p/'Library').mkdir();(p/'Uefi.h').write_text(UEFI)
            for name in ('BaseLib','BaseMemoryLib','DebugLib'):
                (p/'Library'/f'{name}.h').write_text('#include <Uefi.h>\n')
            (p/'test.c').write_text(POLICY_TEST)
            command=[os.environ.get('CC','cc'),'-std=c11','-Wall','-Wextra','-Werror','-fsanitize=undefined','-fno-sanitize-recover=all',f'-I{p}',f'-I{DRIVER}',f'-I{ROOT}/Platform/Radxa/Platforms/CIX/Sky1/Include',str(p/'test.c'),'-o',str(p/'test')]
            subprocess.run(command,check=True)
            subprocess.run([str(p/'test')],check=True)

if __name__=='__main__': unittest.main()
