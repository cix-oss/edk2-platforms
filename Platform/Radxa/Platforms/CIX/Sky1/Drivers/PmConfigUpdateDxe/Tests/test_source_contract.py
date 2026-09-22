"""Check integration and independently declared interfaces without compilation."""
# SPDX-License-Identifier: BSD-2-Clause-Patent
from pathlib import Path
import re
import unittest

DRIVER = Path(__file__).resolve().parent.parent
ROOT = next(parent for parent in DRIVER.parents
            if (parent / "Platform/Radxa").is_dir())
HII = ROOT / "Platform/Radxa/Platforms/CIX/Sky1/Drivers/PlatformConfigDxe"


class SourceContractTests(unittest.TestCase):
    def test_both_boards_include_the_shared_driver(self):
        for board in ("O6", "O6N"):
            dsc = (ROOT / f"Platform/Radxa/Orion/{board}/{board}.dsc").read_text()
            self.assertRegex(dsc, r"DEFINE RADXA_CPU_OC_SUPPORT\s*= TRUE")
            self.assertIn("Platform/Radxa/Platforms/CIX/Sky1/Sky1Common.dsc.inc", dsc)
        for suffix in ("dsc", "fdf"):
            source = (ROOT / f"Platform/Radxa/Platforms/CIX/Sky1/Sky1Common.{suffix}.inc").read_text()
            self.assertIn("Platform/Radxa/Platforms/CIX/Sky1/Drivers/PmConfigUpdateDxe/PmConfigUpdateDxe.inf", source)
            self.assertIn("!if $(RADXA_CPU_OC_SUPPORT) == TRUE", source)
            if suffix == "dsc":
                self.assertIn("-DRADXA_CPU_OC_SUPPORT=1", source)

    def test_hii_defaults_match_policy_domain_arrays(self):
        source = (DRIVER / "PmConfigPolicy.c").read_text()
        domains = re.findall(
            r'"(G[BM][01])",\s*(\d+),\s*(\d+),\s*2,\s*PM_CONFIG_CPU_PROTECTED_INDEX,\s*'
            r'\{([^}]+)\},\s*\{([^}]+)\}', source)
        self.assertEqual(len(domains), 4)
        hii = (HII / "PmMenu/PmConfig.hfr").read_text()
        questions = re.findall(
            r"numeric varid = RadxaCpuOcVar\.(CpuFrequency|CpuVoltage)\[(\d+)\],(.*?)endnumeric;",
            hii, re.S)
        defaults = {(field, int(index)): int(re.search(r"default = (\d+),", body)[1])
                    for field, index, body in questions}
        expected = {}
        for cpu, (_, domain, size, frequencies, voltages) in enumerate(domains):
            self.assertEqual(int(domain), cpu + 3)
            self.assertEqual(int(size), 6 if cpu == 3 else 7)
            for field, values in (("CpuFrequency", frequencies), ("CpuVoltage", voltages)):
                numbers = [int(value.strip()) for value in values.split(",") if value.strip()]
                self.assertEqual(len(numbers), int(size))
                for index, value in enumerate(numbers):
                    expected[(field, cpu * 13 + index)] = value
        self.assertEqual(defaults, expected)

    def test_boot_questions_are_hidden(self):
        hii = (HII / "PmMenu/PmConfig.hfr").read_text()
        blocks = re.findall(r"suppressif TRUE;(.*?)endif;", hii, re.S)
        for index in (2, 15, 28, 41):
            self.assertTrue(any(f"CpuFrequency[{index}]" in block and
                                f"CpuVoltage[{index}]" in block for block in blocks))

    def test_voltage_questions_match_policy_limits(self):
        header = (DRIVER / "PmConfigUpdateDxe.h").read_text()
        lower = int(re.search(r"#define PM_CONFIG_CPU_VOLTAGE_MIN\s+(\d+)U", header)[1])
        upper = int(re.search(r"#define PM_CONFIG_CPU_VOLTAGE_MAX\s+(\d+)U", header)[1])
        hii = (HII / "PmMenu/PmConfig.hfr").read_text()
        questions = re.findall(
            r"numeric varid = RadxaCpuOcVar\.CpuVoltage\[\d+\],(.*?)endnumeric;",
            hii, re.S)
        self.assertEqual(len(questions), 27)
        for question in questions:
            limits = re.search(r"minimum = (\d+), maximum = (\d+), step = (\d+),", question)
            self.assertEqual(tuple(map(int, limits.groups())), (lower, upper, 10))

    def test_mid_and_little_menu_limits(self):
        hii = (HII / "PmMenu/PmConfig.hfr").read_text()
        questions = re.findall(
            r"numeric varid = RadxaCpuOcVar\.CpuFrequency\[\d+\],(.*?)endnumeric;",
            hii, re.S)
        self.assertEqual(len(questions), 27)
        for body in questions:
            self.assertIn("minimum = 800, maximum = 3200, step = 10,", body)
        frequency = re.search(
            r"numeric varid = RadxaCpuOcVar\.LittleMaxFrequency,(.*?)endnumeric;", hii, re.S)[1]
        self.assertIn("minimum = 1800, maximum = 2400, step = 10,", frequency)
        self.assertIn("default = 1800,", frequency)
        voltage = re.search(
            r"numeric varid = RadxaCpuOcVar\.LittleMinVoltage,(.*?)endnumeric;", hii, re.S)[1]
        header = (DRIVER / "PmConfigUpdateDxe.h").read_text()
        lower = int(re.search(r"#define PM_CONFIG_LITTLE_VOLTAGE_MIN\s+(\d+)U", header)[1])
        upper = int(re.search(r"#define PM_CONFIG_LITTLE_VOLTAGE_MAX\s+(\d+)U", header)[1])
        limits = re.search(r"minimum = (\d+), maximum = (\d+), step = (\d+),", voltage)
        minimum, maximum, step = map(int, limits.groups())
        self.assertEqual((minimum, maximum, step), (0, upper, 10))
        self.assertIn("default = 0,", voltage)
        self.assertIn("flags = RESET_REQUIRED,", voltage)
        condition = re.search(
            r"inconsistentif prompt = STRING_TOKEN\(STR_PM_LITTLE_VOLT_INVALID\),\s*(.*?)\s*endif;",
            voltage, re.S)[1]
        expression = compile(condition.replace("AND", "and").replace("OR", "or"),
                             "<LITTLE voltage validation>", "eval")
        accepted = {
            value for value in range(minimum, maximum + 1)
            if not eval(expression, {"__builtins__": {}}, {"pushthis": value})
        }
        self.assertEqual(accepted, {0, *range(lower, upper + 1, 10)})
        mode = re.search(r"oneof varid = RadxaCpuOcVar\.LittleMode,(.*?)endoneof;", hii, re.S)[1]
        self.assertEqual(re.findall(r"value = (\d+),", mode), ["0", "2"])
        self.assertIn("grayoutif ideqval RadxaCpuOcVar.LittleMode == 0;", hii)

    def test_profile_header_matches_hii(self):
        header = (ROOT / "Platform/Radxa/Platforms/CIX/Sky1/Include/RadxaSetupVar.h").read_text()
        revision = int(re.search(r"#define RADXA_PM_TUNING_REVISION\s+(\d+)", header)[1])
        signature = int(re.search(r"#define RADXA_PM_TUNING_SIGNATURE\s+(0x[0-9A-Fa-f]+)", header)[1], 16)
        hii = (HII / "PmMenu/PmConfig.hfr").read_text()
        for field, expected in (("Revision", revision), ("DataSize", 278), ("Signature", signature)):
            body = re.search(rf"numeric varid = RadxaCpuOcVar\.{field},(.*?)endnumeric;", hii, re.S)[1]
            self.assertEqual(int(re.search(r"default = (0x[0-9A-Fa-f]+|\d+),", body)[1], 0), expected)

    def test_distinct_variable_and_unchanged_board_layout(self):
        header = (ROOT / "Platform/Radxa/Platforms/CIX/Sky1/Include/RadxaSetupVar.h").read_text()
        layout = re.search(r"typedef struct \{(.*?)\} RADXA_SETUP_DATA;", header, re.S)[1]
        self.assertEqual(re.findall(r"UINT8\s+(\w+);", layout), ["UFSPowerMode", "EnableAcpiScmi"])
        self.assertIn('L"RadxaCpuOcVar"', header)
        self.assertIn('L"RadxaCpuOcStatusVar"', header)
        self.assertNotIn('L"RadxaPmTuningVar"', header)
        self.assertIn("PM_STATUS_ATTRIBUTES EFI_VARIABLE_BOOTSERVICE_ACCESS", (DRIVER / "PmConfigUpdateDxe.c").read_text())

    def test_default_consumer_is_disabled(self):
        source = (DRIVER / "CixCpuOcExpected.h").read_text()
        self.assertRegex(source, r"#define CIX_CPU_OC_PM_ABI\s+0U")
        driver = (DRIVER / "PmConfigUpdateDxe.c").read_text()
        self.assertIn("PmBl1SelectsExpectedPm (Bootloader)", driver)
        self.assertIn("Sha256HashAll (Bootloader + CIX_CPU_OC_PM_OFFSET", driver)

    def test_public_raw_api_keeps_size_by_value(self):
        source = (ROOT / "Platform/CIX/Sky1/Include/Protocol/CixFwUpdateProtocol.h").read_text()
        prototype = re.search(r"typedef UINT16 \(\*CIX_FIRMWARE_ENTRY_UPDATE\) \((.*?)\);", source, re.S)[1]
        self.assertRegex(prototype, r"UINT32\s+ImageSize,")
        self.assertNotRegex(prototype, r"UINT32\s*\*\s*ImageSize")

    def test_menu_string_tokens_are_resolved(self):
        used = set(re.findall(r"STRING_TOKEN\((STR_PM_\w+)\)", (HII / "PmMenu/PmConfig.hfr").read_text()))
        defined = set(re.findall(r"#string (STR_PM_\w+)", (HII / "PmMenu/PmConfig.uni").read_text()))
        self.assertFalse(used - defined)

    def test_menu_describes_minimum_nominal_requests(self):
        strings = (HII / "PmMenu/PmConfig.uni").read_text()
        self.assertIn('"Custom minimum voltage"', strings)
        self.assertIn('"Minimum nominal voltage, 550-1250 mV', strings)
        self.assertIn('normally +30 mV', strings)
        self.assertIn('O6N fixed DSU/LITTLE', strings)
        self.assertIn('higher requests to be rejected', strings)
        labels = re.findall(r'#string STR_PM_G[BM][01]_\d+_VOLT .*?"([^"]+)"', strings)
        self.assertEqual(len(labels), 27)
        self.assertTrue(all('(min nominal mV)' in label for label in labels))

    def test_boot_report_is_exposed_separately_from_persistence(self):
        header = (ROOT / "Platform/Radxa/Platforms/CIX/Sky1/Include/RadxaSetupVar.h").read_text()
        hii = (HII / "PmMenu/PmConfig.hfr").read_text()
        for field in ("SavedProfile", "State", "LastError", "RuntimeState", "RuntimeReason"):
            self.assertIn(f"RadxaCpuOcStatusVar.{field}", hii)
        status = re.search(r"typedef struct \{([^{}]+)\} RADXA_PM_STATUS_DATA;", header)[1]
        self.assertEqual(re.findall(r"UINT8\s+(\w+);", status), [
            "Revision", "CustomSupported", "SavedProfile", "State", "LastError",
            "RuntimeState", "RuntimeReason"])
        inf = (DRIVER / "PmConfigUpdateDxe.inf").read_text()
        self.assertIn("PmConfigRuntime.c", inf)
        self.assertIn("ArmPkg/ArmPkg.dec", inf)
        self.assertIn("ArmMtlLib", inf)


if __name__ == "__main__":
    unittest.main()
