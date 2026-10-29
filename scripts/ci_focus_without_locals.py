from frappe.testing.result import TestResult
from frappe.utils.bench_helper import main

original_start = TestResult.startTest

def compact_start(self, test):
    original_start(self, test)
    self.tb_locals = False

TestResult.startTest = compact_start
main()
