def dependency_analysis_prompt():
    return """
Return SAP dependency analysis as structured JSON only.

Do not generate any ABAP.
Do not return free-form text.

The JSON must identify only the SAP dependencies required by the functional specification.
Do not design the implementation.
Do not plan declarations, operations, FORM routines, execution order, local variables, internal tables, work areas, output structures, ALV processing, CSV processing, LOOPs, READs, MOVEs, APPENDs, or SELECT statements.
Those implementation details are generated deterministically later from the functional specification and identified dependencies.

Return exactly this JSON shape:
{
  "ddic_objects": [
    {
      "name": "EDIDC",
      "structure": "st_edidc",
      "table": "t_edidc"
    }
  ],
  "callables": [],
  "unresolved": []
}

Field rules:
- ddic_objects: required SAP DDIC tables, structures, and database views only. Each item must be an object with name, structure, and table only.
- For each DDIC object, set structure to "st_" plus the lowercase DDIC object name, and table to "t_" plus the lowercase DDIC object name.
- callables: required function modules, classes, interfaces, and class/interface methods using provider-compatible identities.
- unresolved: missing or ambiguous SAP dependencies only. Do not put implementation tasks in unresolved.

Classification rules:
- Do not put data elements, domains, standalone ABAP types, fields, function-module parameters, or descriptive names in ddic_objects.
- Treat fields used only in SQL WHERE conditions as filter fields, not as fields that must be read, returned, or represented in implementation structures.
- Include every DDIC table or view whose table read is explicitly requested in the specification, even when some or all mentioned fields are used only as filters.
- Preserve explicit dependencies between requested table reads when identifying required DDIC objects.
- Do not classify local structures or variables as SAP DDIC merely because they appear after TYPE or LIKE.
- Function-module names belong in callables, not ddic_objects.
- Return candidates only when the specification provides explicit evidence.
- Ignore local identifiers, generated variable names, FORM names, implementation step names, output field names and business process descriptions.
""".strip()
