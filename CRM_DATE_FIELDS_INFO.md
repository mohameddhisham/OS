# CRM Date Fields Analysis

## Date Fields in CSV

The CRM CSV file contains the following date-related columns:

1. **Assign Date** - When the lead was assigned to an agent
2. **Finish Date** - When the work was completed
3. **Validation Date** - When the record was validated
4. **Date of Sale** - When the sale was made (PRIMARY FIELD FOR ANALYSIS)
5. **Creation Date** - When the record was created
6. **Date of Payment** - When payment was received
7. **Due Date** - Payment due date
8. **Creation Date Auto** - Auto-generated creation timestamp

## Sample Data

From the CSV file (MLA_Campaign (20).csv):

| Column | Sample Value | Description |
|--------|--------------|-------------|
| Assign Date | (empty) | Often empty |
| Finish Date | (empty) | Often empty |
| Validation Date | (empty) | Often empty |
| Date of Sale | 2026-04-23 | **Used for monthly analysis** |
| Creation Date | 2026-04-23 | Record creation date |
| Date of Payment | (empty) | Often empty |
| Due Date | (empty) | Often empty |

## Date Handling in CRM Analysis

### Primary Date Field: Date of Sale
- **Priority**: We specifically look for "Date of Sale" column first
- **Fallback**: If not found, we use any column with "date" in the name
- **Conversion**: Uses `pd.to_datetime(errors='coerce')` to safely parse dates
- **Invalid Dates**: Values like `0000-00-00` are converted to NaT (Not a Time) and excluded

### Date Parsing Strategy
```python
# 1. Look for Date of Sale specifically
if 'date of sale' in col.lower():
    date_col = col

# 2. Fall back to any date column
if not date_col:
    if 'date' in col.lower():
        date_col = col

# 3. Convert with error handling
df[date_col] = pd.to_datetime(df[date_col], errors='coerce')
```

### Why Date of Sale?
- Most reliable date for sales analysis
- Contains actual transaction dates (April 2026 - October 2026)
- Used for monthly filtering and daily trend analysis
- Assign Date, Finish Date, and Validation Date are often empty
- Date of Payment is empty for many records

### Month Filtering
- Dynamically extracts all unique year-month combinations from Date of Sale
- Sorts chronologically (not alphabetically)
- Shows months: April 2026, May 2026, June 2026, July 2026, August 2026, September 2026, October 2026
- When a month is selected, all KPIs are recalculated for that month only

### Daily Analysis
- Groups data by date for daily trend charts
- Shows daily Sales, Transfers, Red Flags, Transferred counts
- Tracks daily conversion rates
- Only includes records with valid Date of Sale values
