# Choose the public Python interface

Type: grilling
Status: open
Blocked by: 01, 02

## Question

What exact public types, functions, classes, methods, import paths, and distribution seams do the three Python packages expose in v0, including the two low-level loop pairs and unavoidable Python keyword or type-system adaptations? If v0 selects public `Agent.continue()`, this ticket owns reserved key `PA:python-illegal-identifier`, must lock its exact one-to-one Python spelling such as `continue_`, and must write the complete seven-field Ledger record; the key cannot authorize any other rename.
