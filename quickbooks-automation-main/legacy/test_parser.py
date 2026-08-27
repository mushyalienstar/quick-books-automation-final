from invoice.parser import print_rows, extract_rows_with_positions

file_path = r"C:\Users\arnav\OneDrive\Documents\Coding\quickbooks-automation\invoices\invoice.pdf"

# Quick readable preview, row by row
print_rows(file_path)

print("\n--- WITH POSITIONS ---\n")

# Detailed view with coordinates, useful for debugging column alignment
extract_rows_with_positions(file_path)
