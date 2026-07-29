************************************************************************
* APPROVED DATABASE ACCESS PATTERNS
************************************************************************

* Initial table read
SELECT field1
       field2
  FROM database_table
  INTO TABLE t_header
  WHERE selection_field IN s_option.

* Dependent table read
IF t_header[] IS NOT INITIAL.

  SELECT field1
         field2
    FROM dependent_table
    INTO TABLE t_items
    FOR ALL ENTRIES IN t_header
    WHERE key_field = t_header-key_field
      AND selection_field IN s_option.

ENDIF.

* Rules demonstrated by this pattern:
*
* - Read each database table with one logical SELECT unless the
*   functional specification explicitly requires otherwise.
*
* - When a database read depends on records already loaded into an
*   internal table, use FOR ALL ENTRIES.
*
* - Always check that the same driving internal table is not initial
*   before FOR ALL ENTRIES.
*
* - Apply selection-options directly in the SQL WHERE clause.
*
* - A blank selection-option means no restriction.
*
* - Do not create branches based only on whether a selection-option
*   is populated.
*
* - Do not generate SQL subqueries, nested SELECT statements,
*   SELECT inside LOOP, or SELECT SINGLE inside LOOP.
*
* - Replace all placeholder object and field names with objects and
*   fields from the functional specification.
*
* - Never return placeholder names as executable ABAP.
