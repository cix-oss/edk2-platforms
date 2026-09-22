"""Exercise persistence, foreign-data preservation and consumer gating using real DXE C.
# SPDX-License-Identifier: BSD-2-Clause-Patent

This test compiles only a native mock harness when explicitly requested.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from test_policy import ROOT, DRIVER, UEFI, POLICY_TEST

SERVICES = r'''
#include <Uefi.h>
#include <setjmp.h>
#include <wchar.h>
#include <Protocol/CixFwUpdateProtocol.h>
#include <Library/ArmMtlLib.h>
#define EFIAPI
#define EFI_ERROR(x) (((x)&(1ULL<<63))!=0)
#define EFI_SUCCESS 0
#define EFI_DEVICE_ERROR ((1ULL<<63)|7)
#define EFI_NOT_FOUND ((1ULL<<63)|14)
#define EFI_COMPROMISED_DATA ((1ULL<<63)|33)
#define EFI_BUFFER_TOO_SMALL ((1ULL<<63)|5)
#define EFI_TIMEOUT ((1ULL<<63)|18)
#define TPL_APPLICATION 4U
#define TPL_NOTIFY 16U
#define EFI_VARIABLE_NON_VOLATILE 1
#define EFI_VARIABLE_BOOTSERVICE_ACCESS 2
#define EfiResetCold 0
#define SHA256_DIGEST_SIZE 32
typedef VOID *EFI_HANDLE;typedef VOID EFI_SYSTEM_TABLE;
#include <RadxaSetupVar.h>
EFI_GUID gRadxaSetupVariableGuid,gCixFirmwareUpdateProtocolGuid;
static UINT8 nv[512],sector[4096];static UINTN nv_size;static RADXA_PM_STATUS_DATA published;
static int status_fail,setting_fail,read_fail,write_fail,verify_fail,alloc_fail,hash_fail,header_fail;
static int writes,resets,allocations,bl1_reads,settings_writes;
static int mtl_failure,mtl_gets,mtl_waits,mtl_sends,mtl_receives,mtl_payload_reads;
static int mtl_pre_send_waits,mtl_pre_receive_waits;
enum {TEST_MTL_GET=1,TEST_MTL_WAIT_BEFORE_SEND,TEST_MTL_SEND,
      TEST_MTL_WAIT_BEFORE_RECEIVE,TEST_MTL_RECEIVE};
static int tpl_raises,tpl_restores,mtl_stage;
static EFI_TPL current_tpl=TPL_APPLICATION;
static MTL_CHANNEL channel;
static UINT32 response[7],response_header,response_length;
static UINT32 nv_attributes;
static jmp_buf reset_jump;
static EFI_STATUS get_variable(const wchar_t *name,EFI_GUID*g,UINT32*a,UINTN*n,VOID*b) {
  (void)g;if(a)*a=nv_attributes;
  if(wcscmp(name,RADXA_PM_STATUS_VAR)==0){if(*n<sizeof(published))return EFI_BUFFER_TOO_SMALL;*n=sizeof(published);memcpy(b,&published,*n);return EFI_SUCCESS;}
  if(!nv_size)return EFI_NOT_FOUND;if(*n<nv_size){*n=nv_size;return EFI_BUFFER_TOO_SMALL;}
  *n=nv_size;memcpy(b,nv,nv_size);return EFI_SUCCESS;
}
static EFI_STATUS set_variable(const wchar_t *name,EFI_GUID*g,UINT32 a,UINTN n,VOID*b) {
  (void)g;
  if(wcscmp(name,RADXA_PM_STATUS_VAR)==0){if(status_fail)return EFI_DEVICE_ERROR;assert(a==EFI_VARIABLE_BOOTSERVICE_ACCESS);assert(n==sizeof(published));memcpy(&published,b,n);return EFI_SUCCESS;}
  if(setting_fail)return EFI_DEVICE_ERROR;assert(a==3);assert(n<=sizeof(nv));settings_writes++;nv_size=n;memcpy(nv,b,n);return EFI_SUCCESS;
}
static VOID reset_system(int kind,EFI_STATUS status,UINTN size,VOID *data) { (void)kind;(void)status;(void)size;(void)data;resets++;longjmp(reset_jump,1); }
static VOID CpuDeadLoop(void) { abort(); }
static void word(UINT8*b,unsigned off,UINT32 value){memcpy(b+off,&value,4);}
static UINT16 raw(UINT8 type,UINT8*b,UINT32 n,ENTRY_UPDATE_METHOD method,EFI_FIRMWARE_MANAGEMENT_UPDATE_IMAGE_PROGRESS cb) {
 (void)cb;
 if(type==1){
   assert(method==ENTRY_READ&&n==16384);bl1_reads++;memset(b,0xa5,n);
   memcpy(b,"CIXBTFF!",8);word(b,12,0);word(b,16,2);word(b,2048,1);word(b,2532,0x5a);word(b,2536,3);
   for(unsigned i=0;i<3;i++){word(b,2540+i*24,i+1);word(b,2552+i*24,(i+1)*4096);word(b,2556+i*24,64);}
   if(header_fail)word(b,2552+24,12288);
   return hash_fail?0x116:0x100;
 }
 assert(type==4&&n==4096);if(method==ENTRY_READ){if(read_fail)return 0x416;memcpy(b,sector,4096);if(verify_fail&&writes)b[0]^=1;return 0x400;}
 assert(method==ENTRY_WRITE);writes++;if(write_fail)return 0x414;memcpy(sector,b,4096);return 0x400;
}
static CIX_FW_UPDATE_PROTOCOL protocol={.Version=3,.FirmwareRawEntryUpdate=raw};
static EFI_STATUS locate(EFI_GUID*g,VOID*r,VOID**p){(void)g;(void)r;*p=&protocol;return EFI_SUCCESS;}
static EFI_TPL raise_tpl(EFI_TPL level) {
 assert(level==TPL_NOTIFY&&current_tpl==TPL_APPLICATION);tpl_raises++;
 EFI_TPL old=current_tpl;current_tpl=level;return old;
}
static VOID restore_tpl(EFI_TPL level) {
 assert(level==TPL_APPLICATION&&current_tpl==TPL_NOTIFY);tpl_restores++;current_tpl=level;
}
EFI_STATUS MtlGetChannel(MTL_CHANNEL_TYPE type,MTL_CHANNEL**out) {
 assert(current_tpl==TPL_NOTIFY&&type==MTL_CHANNEL_TYPE_LOW);mtl_gets++;mtl_stage=TEST_MTL_GET;
 if(mtl_failure==TEST_MTL_GET)return EFI_TIMEOUT;*out=&channel;return EFI_SUCCESS;
}
EFI_STATUS MtlWaitUntilChannelFree(MTL_CHANNEL*c,UINTN timeout) {
 assert(current_tpl==TPL_NOTIFY&&c==&channel&&timeout==20000U);
 assert(mtl_stage==TEST_MTL_GET||mtl_stage==TEST_MTL_SEND);
 if(mtl_stage==TEST_MTL_GET){mtl_pre_send_waits++;mtl_stage=TEST_MTL_WAIT_BEFORE_SEND;}
 else{mtl_pre_receive_waits++;mtl_stage=TEST_MTL_WAIT_BEFORE_RECEIVE;}
 mtl_waits++;return mtl_failure==mtl_stage?EFI_TIMEOUT:EFI_SUCCESS;
}
EFI_STATUS MtlSendMessage(MTL_CHANNEL*c,UINT32 header,UINT32 length) {
 assert(current_tpl==TPL_NOTIFY&&c==&channel&&header==((0x80U<<10)|3U)&&length==0U&&mtl_stage==TEST_MTL_WAIT_BEFORE_SEND);
 mtl_sends++;mtl_stage=TEST_MTL_SEND;return mtl_failure==TEST_MTL_SEND?EFI_TIMEOUT:EFI_SUCCESS;
}
EFI_STATUS MtlReceiveMessage(MTL_CHANNEL*c,UINT32*header,UINT32*length) {
 assert(current_tpl==TPL_NOTIFY&&c==&channel&&mtl_stage==TEST_MTL_WAIT_BEFORE_RECEIVE);
 mtl_receives++;mtl_stage=TEST_MTL_RECEIVE;
 if(mtl_failure==TEST_MTL_RECEIVE)return EFI_TIMEOUT;*header=response_header;*length=response_length;return EFI_SUCCESS;
}
UINT32 *MtlGetChannelPayload(MTL_CHANNEL*c) {
 assert(current_tpl==TPL_NOTIFY&&c==&channel&&mtl_stage==TEST_MTL_RECEIVE);mtl_payload_reads++;return response;
}
static struct {EFI_STATUS(*GetVariable)(const wchar_t*,EFI_GUID*,UINT32*,UINTN*,VOID*);EFI_STATUS(*SetVariable)(const wchar_t*,EFI_GUID*,UINT32,UINTN,VOID*);VOID(*ResetSystem)(int,EFI_STATUS,UINTN,VOID*);} runtime={get_variable,set_variable,reset_system},*gRT=&runtime;
static struct {EFI_STATUS(*LocateProtocol)(EFI_GUID*,VOID*,VOID**);EFI_TPL(*RaiseTPL)(EFI_TPL);VOID(*RestoreTPL)(EFI_TPL);} boot={locate,raise_tpl,restore_tpl},*gBS=&boot;
static VOID *AllocateZeroPool(UINTN n){if(alloc_fail)return NULL;VOID*p=calloc(1,n);if(p)allocations++;return p;}
static VOID FreePool(VOID*p){if(p)allocations--;free(p);}
static BOOLEAN Sha256HashAll(CONST VOID*b,UINTN n,UINT8*d){assert(n==64&&((CONST UINT8*)b)[0]==0xa5);memset(d,hash_fail?0:0x5a,32);return TRUE;}
#include "PmConfigPolicy.c"
#include "PmConfigRuntime.c"
#include "PmConfigUpdateDxe.c"
'''
MTL_HEADER = r'''
#ifndef TEST_ARM_MTL_LIB_H
#define TEST_ARM_MTL_LIB_H
#include <Uefi.h>
typedef enum {MTL_CHANNEL_TYPE_LOW=0,MTL_CHANNEL_TYPE_HIGH=1} MTL_CHANNEL_TYPE;
typedef struct {UINT32 unused;} MTL_CHANNEL;
EFI_STATUS MtlGetChannel(MTL_CHANNEL_TYPE,MTL_CHANNEL**);
EFI_STATUS MtlWaitUntilChannelFree(MTL_CHANNEL*,UINTN);
EFI_STATUS MtlSendMessage(MTL_CHANNEL*,UINT32,UINT32);
EFI_STATUS MtlReceiveMessage(MTL_CHANNEL*,UINT32*,UINT32*);
UINT32 *MtlGetChannelPayload(MTL_CHANNEL*);
#endif
'''
MAIN=r'''
static void answer(UINT32 state,UINT32 reason) {
 memset(response,0,sizeof(response));response[1]=5U;response[2]=state;response[3]=reason;
 memcpy(response+4,sector+PM_CONFIG_LENGTH_OFFSET,4);
 memcpy(response+5,sector+PM_CONFIG_CRC1_OFFSET,4);
 memcpy(response+6,sector+PM_CONFIG_CRC2_OFFSET,4);
 response_header=(0x80U<<10)|3U;response_length=sizeof(response);
}
static void setup(void) {
 assert(current_tpl==TPL_APPLICATION&&tpl_raises==tpl_restores);
 fixture(sector);memset(nv,0,sizeof(nv));nv_size=0;memset(&published,0,sizeof(published));
 status_fail=setting_fail=read_fail=write_fail=verify_fail=alloc_fail=hash_fail=header_fail=writes=resets=bl1_reads=settings_writes=0;nv_attributes=3;protocol.Version=3;assert(!allocations);
 mtl_failure=mtl_gets=mtl_waits=mtl_sends=mtl_receives=mtl_payload_reads=mtl_stage=0;
 mtl_pre_send_waits=mtl_pre_receive_waits=0;
 tpl_raises=tpl_restores=0;answer(RADXA_PM_RUNTIME_UNKNOWN,0U);
}
static void request(void) {RADXA_PM_TUNING_DATA s;PmInitializeSettings(&s);s.Profile=2;memcpy(nv,&s,sizeof(s));nv_size=sizeof(s);}
static void custom_sector(void) {request();assert(PmApplyProfile(sector,(RADXA_PM_TUNING_DATA*)nv));assert(PmValidateConfig(sector));}
static void run(void) {if(!setjmp(reset_jump))PmConfigUpdateDxeEntryPoint(NULL,NULL);assert(!allocations);assert(current_tpl==TPL_APPLICATION&&tpl_raises==tpl_restores);}
static void runtime_result(UINT8 expected,UINT8 reason) {
 RADXA_PM_STATUS_DATA state={0};state.CustomSupported=1U;
 state.RuntimeState=RADXA_PM_RUNTIME_ACCEPTED;state.RuntimeReason=8U;
 PmReadRuntimeStatus(sector,&state);
 assert(state.RuntimeState==expected&&state.RuntimeReason==reason);
 assert(current_tpl==TPL_APPLICATION&&tpl_raises==tpl_restores);
}
#if CIX_CPU_OC_PM_ABI == 5U
static void test_runtime(void) {
 setup();custom_sector();answer(RADXA_PM_RUNTIME_ACCEPTED,0U);
 run();assert(!writes&&!resets&&published.Revision==2U&&published.State==RADXA_PM_STATE_READY);
 assert(published.SavedProfile==2U&&published.RuntimeState==RADXA_PM_RUNTIME_ACCEPTED);
 assert(mtl_gets==1&&mtl_payload_reads==1&&tpl_raises==1&&tpl_restores==1);
 assert(mtl_waits==2&&mtl_pre_send_waits==1&&mtl_pre_receive_waits==1);

 // ABI5 can correlate the older C0 request before upgrading it exactly once.
 setup();custom_sector();sector[152]=0xc0;checksum(sector);answer(RADXA_PM_RUNTIME_ACCEPTED,0U);
 runtime_result(RADXA_PM_RUNTIME_ACCEPTED,0U);run();
 assert(writes==1&&resets==1&&sector[152]==0xc1&&published.RuntimeState==RADXA_PM_RUNTIME_ACCEPTED);
 writes=resets=0;answer(RADXA_PM_RUNTIME_ACCEPTED,0U);run();assert(!writes&&!resets);

 // A saved request rejected by PM remains inspectable, without another write/reset loop.
 setup();custom_sector();answer(RADXA_PM_RUNTIME_REJECTED,RADXA_PM_REJECT_COUPLED_RANGE);
 run();run();assert(!writes&&!resets&&published.State==RADXA_PM_STATE_READY);
 assert(published.SavedProfile==2U&&published.RuntimeState==RADXA_PM_RUNTIME_REJECTED);
 assert(published.RuntimeReason==8U&&mtl_gets==2);
 setup();answer(RADXA_PM_RUNTIME_INACTIVE,0U);run();
 assert(!writes&&!resets&&published.SavedProfile==0U&&published.RuntimeState==RADXA_PM_RUNTIME_INACTIVE);

 // Every failed transport stage must release TPL and stop before the next stage.
 for(int stage=TEST_MTL_GET;stage<=TEST_MTL_RECEIVE;stage++) {
  setup();custom_sector();answer(RADXA_PM_RUNTIME_ACCEPTED,0U);mtl_failure=stage;
  runtime_result(RADXA_PM_RUNTIME_UNKNOWN,0U);
  assert(mtl_gets==1&&mtl_pre_send_waits==(stage>=TEST_MTL_WAIT_BEFORE_SEND));
  assert(mtl_sends==(stage>=TEST_MTL_SEND)&&mtl_pre_receive_waits==(stage>=TEST_MTL_WAIT_BEFORE_RECEIVE));
  assert(mtl_receives==(stage>=TEST_MTL_RECEIVE)&&mtl_waits==mtl_pre_send_waits+mtl_pre_receive_waits);
  assert(!mtl_payload_reads&&tpl_raises==1&&tpl_restores==1);
  // A timed-out reply must never enter the pinned binary's 200-second receive wait.
  if(stage==TEST_MTL_WAIT_BEFORE_RECEIVE)assert(mtl_sends==1&&mtl_waits==2&&!mtl_receives);
 }
 const UINT32 malformed[][2]={{0,1U},{1,4U},{1,6U},{2,4U},{2,0xffffffffU},
                            {3,9U},{3,8U}};
 for(unsigned i=0;i<ARRAY_SIZE(malformed);i++) {
  setup();custom_sector();answer(RADXA_PM_RUNTIME_ACCEPTED,0U);
  response[malformed[i][0]]=malformed[i][1];runtime_result(RADXA_PM_RUNTIME_UNKNOWN,0U);
 }
 setup();custom_sector();answer(RADXA_PM_RUNTIME_REJECTED,0U);runtime_result(RADXA_PM_RUNTIME_UNKNOWN,0U);
 setup();custom_sector();answer(RADXA_PM_RUNTIME_UNKNOWN,8U);runtime_result(RADXA_PM_RUNTIME_UNKNOWN,0U);
 setup();custom_sector();answer(RADXA_PM_RUNTIME_UNKNOWN,0U);runtime_result(RADXA_PM_RUNTIME_UNKNOWN,0U);
 const UINT32 headers[]={((0x80U<<10)|2U),((0x81U<<10)|3U),((0x80U<<10)|3U|(1U<<18))};
 for(unsigned i=0;i<ARRAY_SIZE(headers);i++) {
  setup();custom_sector();answer(RADXA_PM_RUNTIME_ACCEPTED,0U);response_header=headers[i];
  runtime_result(RADXA_PM_RUNTIME_UNKNOWN,0U);assert(!mtl_payload_reads);
 }
 const UINT32 lengths[]={0U,24U,32U};
 for(unsigned i=0;i<ARRAY_SIZE(lengths);i++) {
  setup();custom_sector();answer(RADXA_PM_RUNTIME_ACCEPTED,0U);response_length=lengths[i];
  runtime_result(RADXA_PM_RUNTIME_UNKNOWN,0U);assert(!mtl_payload_reads);
 }
 for(unsigned i=4;i<=6;i++) {
  setup();custom_sector();answer(RADXA_PM_RUNTIME_ACCEPTED,0U);response[i]^=1U;
  runtime_result(RADXA_PM_RUNTIME_DIFFERENT_CONFIG,0U);
 }
 setup();custom_sector();answer(RADXA_PM_RUNTIME_REJECTED,8U);response[5]^=1U;
 runtime_result(RADXA_PM_RUNTIME_DIFFERENT_CONFIG,8U);
 setup();custom_sector();answer(RADXA_PM_RUNTIME_ACCEPTED,0U);sector[4095]^=1U;
 runtime_result(RADXA_PM_RUNTIME_UNKNOWN,0U);
 setup();custom_sector();answer(RADXA_PM_RUNTIME_ACCEPTED,0U);sector[0]^=1U;
 runtime_result(RADXA_PM_RUNTIME_UNKNOWN,0U);

 // An accepted reply cannot describe a native saved profile, even with matching checksums.
 setup();answer(RADXA_PM_RUNTIME_ACCEPTED,0U);run();
 assert(!writes&&!resets&&published.SavedProfile==0U&&published.RuntimeState==RADXA_PM_RUNTIME_UNKNOWN);
 setup();custom_sector();answer(RADXA_PM_RUNTIME_INACTIVE,0U);runtime_result(RADXA_PM_RUNTIME_UNKNOWN,0U);
 setup();request();answer(RADXA_PM_RUNTIME_INACTIVE,0U);run();
 assert(writes==1&&resets==1&&published.State==RADXA_PM_STATE_RESET_PENDING);
 assert(published.RuntimeState==RADXA_PM_RUNTIME_INACTIVE);
}
#endif
int main(void) {
 setup();run();assert(!writes&&!resets&&published.LastError==0&&published.SavedProfile==0&&nv_size==278);
 // Only exact valid old records migrate; migration itself does not alter flash.
 setup();request();RADXA_PM_TUNING_DATA *legacy=(RADXA_PM_TUNING_DATA*)nv;
 legacy->Profile=0;legacy->Revision=1;legacy->DataSize=273;legacy->CpuFrequency[32]=2600;nv_size=273;
 UINT8 old_prefix[265];memcpy(old_prefix,nv,sizeof(old_prefix));run();
 assert(!writes&&!resets&&settings_writes==1&&nv_size==278&&published.LastError==0);
 assert(!memcmp(nv,old_prefix,sizeof(old_prefix))&&legacy->Revision==2&&legacy->DataSize==278);
 assert(legacy->LittleMode==0&&legacy->LittleMaxFrequency==1800&&legacy->LittleMinVoltage==0);
 run();assert(settings_writes==1);
 // An old Vendor record may retain invalid inactive edits. Migrate it intact,
 // then permit restoring either C0 or C1. Re-enabling Custom validates the edits.
 for(unsigned selector=0xc0;selector<=0xc1;selector++) {
  setup();custom_sector();sector[152]=(UINT8)selector;checksum(sector);
  legacy=(RADXA_PM_TUNING_DATA*)nv;legacy->Profile=0;legacy->Revision=1;
  legacy->DataSize=273;legacy->CpuFrequency[32]=65535;legacy->CpuFrequency[6]=800;
  legacy->CpuVoltage[2]=0;legacy->Reserved[0]=255;legacy->CpuVoltageMode[1]=255;nv_size=273;
  memcpy(old_prefix,nv,sizeof(old_prefix));run();
  assert(settings_writes==1&&writes==1&&resets==1&&nv_size==278&&sector[152]==1);
  assert(!memcmp(nv,old_prefix,sizeof(old_prefix))&&legacy->Revision==2&&legacy->DataSize==278);
  writes=resets=0;run();assert(!writes&&!resets&&settings_writes==1&&published.LastError==0);
  legacy->Profile=2;run();assert(!writes&&!resets&&published.LastError==RADXA_PM_ERROR_SETTINGS);
 }
 // An invalid old record, interrupted size, foreign attributes or future version is untouched.
 for(unsigned bad=0;bad<7;bad++) {
  setup();request();legacy=(RADXA_PM_TUNING_DATA*)nv;legacy->Revision=1;legacy->DataSize=273;nv_size=273;
  switch(bad) {
  case 0:legacy->CpuFrequency[32]=2610;break;case 1:legacy->Revision=2;break;
  case 2:legacy->DataSize=278;break;case 3:nv_size=272;break;
  case 4:nv_size=279;break;case 5:nv_attributes=7;break;default:legacy->Signature=0;break;
  }
  UINT8 before[512];memcpy(before,nv,sizeof(nv));UINTN old_size=nv_size;run();
  assert(!writes&&!resets&&!settings_writes&&nv_size==old_size&&!memcmp(before,nv,sizeof(nv)));
  assert(published.LastError==RADXA_PM_ERROR_SETTINGS);
 }
 setup();request();legacy=(RADXA_PM_TUNING_DATA*)nv;legacy->Revision=1;legacy->DataSize=273;nv_size=273;
 setting_fail=1;run();assert(!writes&&!resets&&nv_size==273&&published.LastError==RADXA_PM_ERROR_SETTINGS);
#if CIX_CPU_OC_PM_ABI == 5U
 setup();custom_sector();sector[152]=0xc0;checksum(sector);
 legacy=(RADXA_PM_TUNING_DATA*)nv;legacy->Revision=1;legacy->DataSize=273;nv_size=273;
 answer(RADXA_PM_RUNTIME_ACCEPTED,0U);run();
 assert(settings_writes==1&&writes==1&&resets==1&&nv_size==278&&sector[152]==0xc1);
 writes=resets=0;answer(RADXA_PM_RUNTIME_ACCEPTED,0U);run();assert(!writes&&!resets&&settings_writes==1);
 // The new scalar request is persisted, read back, and does not cause a reset loop.
 setup();request();legacy=(RADXA_PM_TUNING_DATA*)nv;legacy->LittleMode=2;
 legacy->LittleMaxFrequency=2400;legacy->LittleMinVoltage=950;legacy->CpuFrequency[32]=3200;
 run();assert(writes==1&&resets==1&&PmValidateConfig(sector));
 assert(PmRead32(sector,PM_CONFIG_OPP_ENTRY_OFFSET(2,0))==2400);
 writes=resets=0;answer(RADXA_PM_RUNTIME_ACCEPTED,0U);run();assert(!writes&&!resets);
#endif
 setup();sector[152]=0;checksum(sector);run();assert(!writes&&!resets&&sector[152]==0&&published.LastError==RADXA_PM_ERROR_FOREIGN);
 setup();sector[152]=0x80;checksum(sector);run();assert(!writes&&!resets&&sector[152]==0x80&&published.LastError==RADXA_PM_ERROR_FOREIGN);
 setup();nv[0]=2;nv_size=1;run();assert(!writes&&!resets&&nv_size==1&&published.LastError==RADXA_PM_ERROR_SETTINGS);
 setup();request();((RADXA_PM_TUNING_DATA*)nv)->Revision=5;run();assert(!writes&&published.LastError==RADXA_PM_ERROR_SETTINGS&&((RADXA_PM_TUNING_DATA*)nv)->Revision==5);
 setup();read_fail=1;run();assert(!writes&&published.LastError==RADXA_PM_ERROR_READ);
 setup();sector[0]^=1;run();assert(!writes&&published.LastError==RADXA_PM_ERROR_CONFIG);
 setup();alloc_fail=1;run();assert(!writes&&published.LastError==RADXA_PM_ERROR_MEMORY);
 setup();setting_fail=1;run();assert(!writes&&published.LastError==RADXA_PM_ERROR_SETTINGS);
 setup();sector[152]=0xc0;checksum(sector);status_fail=1;run();assert(!writes&&!resets);
 setup();sector[152]=0xc0;checksum(sector);write_fail=1;run();assert(writes==1&&!resets&&published.LastError==RADXA_PM_ERROR_WRITE&&published.SavedProfile==255);
 setup();sector[152]=0xc0;checksum(sector);verify_fail=1;run();assert(writes==1&&!resets&&published.LastError==RADXA_PM_ERROR_VERIFY&&published.SavedProfile==255);
 setup();request();((RADXA_PM_TUNING_DATA*)nv)->Revision=0;run();assert(!writes&&published.LastError==RADXA_PM_ERROR_SETTINGS);
 setup();nv_size=274;run();assert(!writes&&published.LastError==RADXA_PM_ERROR_SETTINGS&&nv_size==274);
 setup();request();((RADXA_PM_TUNING_DATA*)nv)->Signature=0;run();assert(!writes&&published.LastError==RADXA_PM_ERROR_SETTINGS);
 setup();sector[152]=0xc0;checksum(sector);run();assert(writes==1&&resets==1&&sector[152]==1);
 setup();request();nv_attributes=7;run();assert(!writes&&published.LastError==RADXA_PM_ERROR_SETTINGS);
 setup();protocol.Version=2;run();assert(!writes&&!bl1_reads&&published.LastError==RADXA_PM_ERROR_UNSUPPORTED);
 setup();sector[152]=0xc0;word(sector,153,0);checksum(sector);run();assert(!writes&&!resets&&published.LastError==RADXA_PM_ERROR_FOREIGN);
 setup();request();((RADXA_PM_TUNING_DATA*)nv)->CpuVoltage[6]=1250;run();
#if CIX_CPU_OC_PM_ABI == 5U
 assert(writes==1&&resets==1&&sector[152]==0xc1&&bl1_reads==1);
 UINT32 encoded_mv;memcpy(&encoded_mv,sector+153+3*212+4+6*16+4,4);assert(encoded_mv==1250);
 writes=resets=0;answer(RADXA_PM_RUNTIME_ACCEPTED,0U);run();assert(!writes&&!resets&&published.SavedProfile==2&&published.State==RADXA_PM_STATE_READY&&published.RuntimeState==RADXA_PM_RUNTIME_ACCEPTED);
 setup();request();hash_fail=1;run();assert(!writes&&!mtl_gets&&published.LastError==RADXA_PM_ERROR_UNSUPPORTED);
 setup();request();header_fail=1;run();assert(!writes&&!mtl_gets&&published.LastError==RADXA_PM_ERROR_UNSUPPORTED);
 setup();request();word(sector,24,0);checksum(sector);run();assert(!writes&&published.LastError==RADXA_PM_ERROR_CONFIG);
 test_runtime();
#else
 assert(!writes&&!resets&&!bl1_reads&&!mtl_gets&&!tpl_raises&&published.LastError==RADXA_PM_ERROR_UNSUPPORTED);
 setup();custom_sector();answer(RADXA_PM_RUNTIME_ACCEPTED,0U);runtime_result(RADXA_PM_RUNTIME_UNKNOWN,0U);assert(!mtl_gets&&!tpl_raises);
#endif
 setup();RADXA_PM_STATUS_DATA unsupported={0};unsupported.RuntimeState=RADXA_PM_RUNTIME_ACCEPTED;unsupported.RuntimeReason=8U;
 PmReadRuntimeStatus(sector,&unsupported);assert(unsupported.RuntimeState==RADXA_PM_RUNTIME_UNKNOWN&&unsupported.RuntimeReason==0U);
 assert(!mtl_gets&&!tpl_raises&&!tpl_restores);
 return 0;
}
'''

@unittest.skipUnless(os.environ.get("CIX_RUN_HOST_C_TESTS") == "1",
                     "host C compilation requires explicit opt-in")
class DriverTests(unittest.TestCase):
    def test_actual_driver(self):
        for abi in (0,1,2,3,4,5):
            with self.subTest(abi=abi), tempfile.TemporaryDirectory(prefix='cix-dxe-test-') as tmp:
                p=Path(tmp);(p/'Library').mkdir();(p/'Protocol').mkdir()
                (p/'Uefi.h').write_text(UEFI.replace('#define IN','#define EFIAPI\ntypedef UINT64 EFI_STATUS;\ntypedef UINTN EFI_TPL;\n#define IN'))
                for name in ('BaseLib','BaseMemoryLib','DebugLib','BaseCryptLib','MemoryAllocationLib','UefiBootServicesTableLib','UefiRuntimeServicesTableLib'):
                    (p/'Library'/f'{name}.h').write_text('#include <Uefi.h>\n')
                (p/'Library/ArmMtlLib.h').write_text(MTL_HEADER)
                (p/'Protocol/FirmwareManagement.h').write_text('typedef void *EFI_FIRMWARE_MANAGEMENT_UPDATE_IMAGE_PROGRESS;\n')
                for name in ('PmConfigPolicy.c','PmConfigRuntime.c','PmConfigUpdateDxe.c','PmConfigUpdateDxe.h','CixCpuOcExpected.h'):
                    shutil.copyfile(DRIVER/name,p/name)
                if abi:
                    (p/'CixCpuOcExpected.h').write_text('#define CIX_CPU_OC_PM_ABI '+str(abi)+'U\n#define CIX_CPU_OC_BL1_SIZE 16384U\n#define CIX_CPU_OC_PM_OFFSET 8192U\n#define CIX_CPU_OC_PM_SIZE 64U\n#define CIX_CPU_OC_PM_SHA256 {'+','.join(['0x5a']*32)+'}\n')
                fixture=POLICY_TEST[POLICY_TEST.index('static void checksum'):POLICY_TEST.index('static void invalid_word')]
                (p/'test.c').write_text(SERVICES+fixture+MAIN)
                subprocess.run([os.environ.get('CC','cc'),'-std=c11','-Wall','-Wextra','-Werror','-Wno-unused-parameter','-Wno-unused-function','-Wno-misleading-indentation','-fsanitize=undefined','-fno-sanitize-recover=all',f'-I{p}',f'-I{ROOT}/Platform/Radxa/Platforms/CIX/Sky1/Include',f'-I{ROOT}/Platform/CIX/Sky1/Include',str(p/'test.c'),'-o',str(p/'test')],check=True)
                subprocess.run([str(p/'test')],check=True)

if __name__=='__main__':unittest.main()
