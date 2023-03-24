import frappe
from frappe import _


def create_tax_invoice_on_gl_tax(doc, method):

    def create_tax_invoice(doc, doctype, base_amount, tax_amount, voucher):
        # For sales invoice / purchase invoice / payment, we can get the party from GL
        gl = frappe.db.get_list(
            'GL Entry',
            filters={'voucher_type': doc.voucher_type, 'voucher_no': doc.voucher_no, 'party': ['!=', '']},
            fields=['party', 'against_voucher_type', 'against_voucher'],
        )
        # Case use Journal Entry to clear tax, get from the Bill No. field.
        if not gl:
            je = frappe.get_doc(doc.voucher_type, doc.voucher_no)
            if not je.bill_no:
                frappe.throw(_('Please fill in Bill No. (PI/SI/EX/JE) for this Tax Invoice'))
            gl = frappe.db.get_list(
                'GL Entry',
                filters={'voucher_no': je.bill_no, 'party': ['!=', '']},
                fields=['party', 'against_voucher_type', 'against_voucher'],
            )
        if not gl:
            frappe.throw(_('Cannot find against voucher for Tax Invoice'))
        party = gl[0]['party']
        against_voucher_type = gl[0]['against_voucher_type']
        against_voucher = gl[0]['against_voucher']
        # Case expense claim, partner is not employee, but the supplier, correct it first.
        if doc.voucher_type == 'Expense Claim':
            if not voucher.supplier:
                frappe.throw(_('Please fill in Supplier for Purchase Tax Invoice'))
            party = voucher.supplier
        # Create Tax Invoice
        tinv = frappe.get_doc({
            'doctype': doctype,
            'gl_entry': doc.name,
            'tax_amount': tax_amount,
            'tax_base': base_amount,
            'party': party,
            'against_voucher_type': against_voucher_type,
            'against_voucher': against_voucher,
        })
        tinv.insert(ignore_permissions=True)
        return tinv
        
    def update_voucher_tinv(doctype, voucher, tinv):
        # Set company tax address
        def update_company_tax_address(voucher, tinv):
            # From Sales Invoice and Purchase Invoice, use voucher address
            if tinv.voucher_type == "Sales Invoice":
                tinv.company_tax_address = voucher.company_address
            elif tinv.voucher_type == "Purchase Invoice":
                tinv.company_tax_address = voucher.billing_address
            else:  # From Payment Entry, Expense Claim and Journal Entry
                tinv.company_tax_address = voucher.company_tax_address
            if not tinv.company_tax_address:
                frappe.throw(_('No Company Billing/Tax Address'))

        update_company_tax_address(voucher, tinv)

        # Sales Invoice - use Sales Tax Invoice as Tax Invoice
        # Purchase Invoice - use Bill No as Tax Invoice
        if doctype == 'Sales Tax Invoice':
            voucher.tax_invoice_number = tinv.name
            voucher.tax_invoice_date = tinv.date
            tinv.report_date = tinv.date
        if doctype == 'Purchase Tax Invoice':
            if not (voucher.tax_invoice_number and voucher.tax_invoice_date):
                frappe.throw(_('Please enter Tax Invoice Number / Tax Invoice Date'))
            voucher.save()
            tinv.number = voucher.tax_invoice_number
            tinv.report_date = tinv.date = voucher.tax_invoice_date
        voucher.save()
        tinv.save()
        return tinv

    # Auto create Tax Invoice only when account equal to tax account.
    setting = frappe.get_doc('Tax Invoice Settings')
    doctype = False
    tax_amount = 0.0
    voucher = frappe.get_doc(doc.voucher_type, doc.voucher_no)
    is_return = False
    if doc.voucher_type in ['Sales Invoice', 'Purchase Invoice']:
        is_return = voucher.is_return  # Case Debit/Credit Note
    sign = is_return and -1 or 1
    # Tax amount, use Dr/Cr to ensure it support every case
    if doc.account in [setting.sales_tax_account, setting.purchase_tax_account]:
        tax_amount = doc.credit - doc.debit
        if (tax_amount > 0 and not is_return) or (tax_amount < 0 and is_return):
            doctype = 'Sales Tax Invoice'
        if (tax_amount < 0 and not is_return) or (tax_amount > 0 and is_return):
            doctype = 'Purchase Tax Invoice'
        tax_amount = abs(tax_amount) * sign
    if doctype:
        voucher = frappe.get_doc(doc.voucher_type, doc.voucher_no)
        if voucher.docstatus == 2:
            tax_amount = 0
        if tax_amount != 0:
            # Base amount, use base amount from origin document
            if voucher.doctype == 'Expense Claim':
                base_amount = voucher.total_sanctioned_amount
            elif voucher.doctype in ['Purchase Invoice', 'Sales Invoice']:
                base_amount = voucher.base_net_total
            elif voucher.doctype == 'Payment Entry':
                tax = list(filter(lambda x: x.account_head == doc.account, voucher.taxes))
                base_amount = tax and tax[0].base_total - tax[0].base_tax_amount or 0
            elif voucher.doctype == 'Journal Entry':
                base_amount = 0
                # TODO: base_amount = tax_amount * 100 / tax_account.tax_rate,
            base_amount = abs(base_amount) * sign
            tinv = create_tax_invoice(doc, doctype, base_amount, tax_amount, voucher)
            tinv = update_voucher_tinv(doctype, voucher, tinv)
            tinv.submit()


def validate_company_address(doc, method):
    if not doc.company_tax_address:
        addresses = frappe.db.get_list(
            "Address",
            filters={"is_your_company_address": 1, "address_type": "Billing"},
            fields=["name", "address_type"],
        )
        if len(addresses) == 1:
            doc.company_tax_address = addresses[0]["name"]


def validate_tax_invoice(doc, method):
    # If taxes contain tax account, tax invoice is required.
    tax_account = frappe.db.get_single_value("Tax Invoice Settings", "purchase_tax_account")
    voucher = frappe.get_doc(doc.doctype, doc.name)
    for tax in voucher.taxes:
        if tax.account_head == tax_account and not doc.tax_invoice_number:
            frappe.throw(_("This document require Tax Invoice Number"))
