REPORT zabap_builder_json_generator NO STANDARD PAGE HEADING MESSAGE-ID 38.

*---------------------------------------------------------------------*
* ABAP Builder JSON Generator
*
* This program only generates JSON.
* It does not extract metadata from SAP.
*
* Populate:
*
*   DDIC:
*     gs_ddic
*     gt_ddic_fields
*
*   Function module:
*     gt_parameters
*
*   Class method:
*     gs_method
*     gt_parameters
*
* Then call:
*
*   PERFORM build_ddic_json.
*   PERFORM build_function_json.
*   PERFORM build_method_json.
*
* Result:
*   gt_json
*---------------------------------------------------------------------*

*---------------------------------------------------------------------*
* Types
*---------------------------------------------------------------------*
TYPES:
  ty_t_string TYPE STANDARD TABLE OF string.

*---------------------------------------------------------------------*
* DDIC metadata
*---------------------------------------------------------------------*
TYPES:
  BEGIN OF ty_ddic_header,
    name TYPE string,
  END OF ty_ddic_header.

TYPES:
  BEGIN OF ty_ddic_field,
    name        TYPE string,
    rollname    TYPE string,
    datatype    TYPE string,
    length      TYPE i,
    decimals    TYPE i,
    description TYPE string,
    key_flag    TYPE c LENGTH 1,
  END OF ty_ddic_field.

TYPES:
  ty_t_ddic_field TYPE STANDARD TABLE OF ty_ddic_field.

*---------------------------------------------------------------------*
* Function and method parameter metadata
*---------------------------------------------------------------------*
TYPES:
  BEGIN OF ty_parameter,
    name          TYPE string,
    direction     TYPE string,
    raw_direction TYPE string,
    abap_type     TYPE string,
    field_name    TYPE string,
    required      TYPE c LENGTH 1,
  END OF ty_parameter.

TYPES:
  ty_t_parameter TYPE STANDARD TABLE OF ty_parameter.

*---------------------------------------------------------------------*
* Class method metadata
*---------------------------------------------------------------------*
TYPES:
  BEGIN OF ty_method,
    class_name  TYPE string,
    method_name TYPE string,
  END OF ty_method.

*---------------------------------------------------------------------*
* Global data
*---------------------------------------------------------------------*
DATA:
  gs_ddic       TYPE ty_ddic_header,
  gt_ddic_fields TYPE ty_t_ddic_field,
  gs_method     TYPE ty_method,
  gt_parameters TYPE ty_t_parameter,
  gt_json       TYPE ty_t_string.

DATA: t_seosubcodf   TYPE STANDARD TABLE OF seosubcodf,
      t_params       TYPE STANDARD TABLE OF rfc_funint,
      t_rsexc        TYPE STANDARD TABLE OF rsexc,
      t_dfies        TYPE STANDARD TABLE OF dfies,
      t_fixed_values TYPE ddfixvalues.

DATA: st_seosubcodf   TYPE seosubcodf,
      st_params       TYPE rfc_funint,
      st_rsexc        TYPE rsexc,
      st_dfies        TYPE dfies,
      st_fixed_values TYPE ddfixvalues.


DATA: w_remote_basxml_supported TYPE  rs38l-basxml_enabled,
      w_remote_call             TYPE  rs38l-remote,
      w_update_task             TYPE  rs38l-utask,
      w_params                  TYPE  bgrfc_funint_t,
      w_resumable_exceptions    TYPE  siw_tab_rsexc.

SELECTION-SCREEN BEGIN OF BLOCK b1 WITH FRAME TITLE text-s01.
PARAMETERS: p_table TYPE tabnam.
PARAMETERS: p_func TYPE rs38l_fnam.
PARAMETERS: p_class TYPE seoclsname.
PARAMETERS: p_method TYPE seocpdname.
SELECTION-SCREEN END OF BLOCK b1.

SELECTION-SCREEN BEGIN OF BLOCK b2 WITH FRAME TITLE text-s02.
PARAMETERS: r_table RADIOBUTTON GROUP x.
PARAMETERS: r_func  RADIOBUTTON GROUP x.
PARAMETERS: r_class RADIOBUTTON GROUP x.
SELECTION-SCREEN END OF BLOCK b2.

SELECTION-SCREEN BEGIN OF BLOCK b3 WITH FRAME TITLE text-s03.
PARAMETERS: r_rep  RADIOBUTTON GROUP xx.
PARAMETERS: r_file RADIOBUTTON GROUP xx.
SELECTION-SCREEN END OF BLOCK b3.




*---------------------------------------------------------------------*
* Example entry point
*---------------------------------------------------------------------*
START-OF-SELECTION.

  CASE 'X'.
    WHEN r_table.
      PERFORM read_table.
      PERFORM build_ddic_json.
    WHEN r_func.
      PERFORM read_func.
      PERFORM build_function_json.
    WHEN r_class.
      PERFORM read_class_method.
      PERFORM build_method_json.
  ENDCASE.

END-OF-SELECTION.

  IF NOT r_rep IS INITIAL.
    PERFORM output_json.
  ELSE.
    PERFORM download_json.
  ENDIF.

*---------------------------------------------------------------------*
* Build DDIC JSON
*---------------------------------------------------------------------*
FORM build_ddic_json.

  DATA:
    ls_field       TYPE ty_ddic_field,
    lv_field_count TYPE i,
    lv_field_index TYPE i,
    lv_line(100),
    lv_name        TYPE string,
    lv_rollname    TYPE string,
    lv_datatype    TYPE string,
    lv_description TYPE string,
    lv_length(100),
    lv_decimals(100),
    lv_key(100),
    lv_comma       TYPE c LENGTH 1.

  REFRESH gt_json.

  DESCRIBE TABLE gt_ddic_fields LINES lv_field_count.

  PERFORM json_add_line USING '{'.

*---------------------------------------------------------------------*
* Object name
*---------------------------------------------------------------------*
  PERFORM json_escape
    USING gs_ddic-name
    CHANGING lv_name.

  CONCATENATE
    '  "name": "'
    p_table
    '",'
    INTO lv_line.

  PERFORM json_add_line USING lv_line.

*---------------------------------------------------------------------*
* Field count
*---------------------------------------------------------------------*
  WRITE lv_field_count TO lv_line LEFT-JUSTIFIED.
  CONDENSE lv_line NO-GAPS.

  CONCATENATE
    '  "field_count": '
    lv_line
    ','
    INTO lv_line.

  PERFORM json_add_line USING lv_line.

*---------------------------------------------------------------------*
* Fields object
*---------------------------------------------------------------------*
  PERFORM json_add_line USING '  "fields": {'.

  CLEAR lv_field_index.

  LOOP AT gt_ddic_fields INTO ls_field.

    ADD 1 TO lv_field_index.

    PERFORM json_escape
      USING ls_field-name
      CHANGING lv_name.

    PERFORM json_escape
      USING ls_field-rollname
      CHANGING lv_rollname.

    PERFORM json_escape
      USING ls_field-datatype
      CHANGING lv_datatype.

    PERFORM json_escape
      USING ls_field-description
      CHANGING lv_description.

    WRITE ls_field-length TO lv_length LEFT-JUSTIFIED.
    CONDENSE lv_length NO-GAPS.

    WRITE ls_field-decimals TO lv_decimals LEFT-JUSTIFIED.
    CONDENSE lv_decimals NO-GAPS.

    IF ls_field-key_flag = 'X'.
      lv_key = 'true'.
    ELSE.
      lv_key = 'false'.
    ENDIF.

    CONCATENATE
      '    "'
      lv_name
      '": {'
      INTO lv_line.

    PERFORM json_add_line USING lv_line.

    CONCATENATE
      '      "name": "'
      lv_name
      '",'
      INTO lv_line.

    PERFORM json_add_line USING lv_line.

    CONCATENATE
      '      "rollname": "'
      lv_rollname
      '",'
      INTO lv_line.

    PERFORM json_add_line USING lv_line.

    CONCATENATE
      '      "datatype": "'
      lv_datatype
      '",'
      INTO lv_line.

    PERFORM json_add_line USING lv_line.

    CONCATENATE
      '      "length": '
      lv_length
      ','
      INTO lv_line.

    PERFORM json_add_line USING lv_line.

    CONCATENATE
      '      "decimals": '
      lv_decimals
      ','
      INTO lv_line.

    PERFORM json_add_line USING lv_line.

    CONCATENATE
      '      "description": "'
      lv_description
      '",'
      INTO lv_line.

    PERFORM json_add_line USING lv_line.

    CONCATENATE
      '      "key": '
      lv_key
      INTO lv_line.

    PERFORM json_add_line USING lv_line.

    IF lv_field_index < lv_field_count.
      lv_comma = ','.
    ELSE.
      CLEAR lv_comma.
    ENDIF.

    CONCATENATE
      '    }'
      lv_comma
      INTO lv_line.

    PERFORM json_add_line USING lv_line.

  ENDLOOP.

  PERFORM json_add_line USING '  },'.

*---------------------------------------------------------------------*
* Field order
*---------------------------------------------------------------------*
  PERFORM json_add_line USING '  "field_order": ['.

  CLEAR lv_field_index.

  LOOP AT gt_ddic_fields INTO ls_field.

    ADD 1 TO lv_field_index.

    PERFORM json_escape
      USING ls_field-name
      CHANGING lv_name.

    IF lv_field_index < lv_field_count.
      lv_comma = ','.
    ELSE.
      CLEAR lv_comma.
    ENDIF.

    CONCATENATE
      '    "'
      lv_name
      '"'
      lv_comma
      INTO lv_line.

    PERFORM json_add_line USING lv_line.

  ENDLOOP.

  PERFORM json_add_line USING '  ]'.
  PERFORM json_add_line USING '}'.

ENDFORM.                    "build_ddic_json

*---------------------------------------------------------------------*
* Build function module JSON
*---------------------------------------------------------------------*
FORM build_function_json.

  DATA:
    ls_parameter TYPE ty_parameter,
    lv_count     TYPE i,
    lv_index     TYPE i,
    lv_line      TYPE string,
    lv_name      TYPE string,
    lv_direction TYPE string,
    lv_raw       TYPE string,
    lv_type      TYPE string,
    lv_field     TYPE string,
    lv_required  TYPE string,
    lv_comma     TYPE c LENGTH 1.

  REFRESH gt_json.

  DESCRIBE TABLE gt_parameters LINES lv_count.

  PERFORM json_add_line USING '{'.
  PERFORM json_add_line USING '  "parameters": {'.

  CLEAR lv_index.

  LOOP AT gt_parameters INTO ls_parameter.

    ADD 1 TO lv_index.

    PERFORM json_escape
      USING ls_parameter-name
      CHANGING lv_name.

    PERFORM json_escape
      USING ls_parameter-direction
      CHANGING lv_direction.

    PERFORM json_escape
      USING ls_parameter-raw_direction
      CHANGING lv_raw.

    PERFORM json_escape
      USING ls_parameter-abap_type
      CHANGING lv_type.

    PERFORM json_escape
      USING ls_parameter-field_name
      CHANGING lv_field.

    PERFORM get_json_boolean
      USING ls_parameter-required
      CHANGING lv_required.

    CONCATENATE
      '    "'
      lv_name
      '": {'
      INTO lv_line.

    PERFORM json_add_line USING lv_line.

    CONCATENATE
      '      "direction": "'
      lv_direction
      '",'
      INTO lv_line.

    PERFORM json_add_line USING lv_line.

    CONCATENATE
      '      "rawDirection": "'
      lv_raw
      '",'
      INTO lv_line.

    PERFORM json_add_line USING lv_line.

    CONCATENATE
      '      "abap_type": "'
      lv_type
      '",'
      INTO lv_line.

    PERFORM json_add_line USING lv_line.

    CONCATENATE
      '      "field": "'
      lv_field
      '",'
      INTO lv_line.

    PERFORM json_add_line USING lv_line.

    CONCATENATE
      '      "required": '
      lv_required
      INTO lv_line.

    PERFORM json_add_line USING lv_line.

    IF lv_index < lv_count.
      lv_comma = ','.
    ELSE.
      CLEAR lv_comma.
    ENDIF.

    CONCATENATE
      '    }'
      lv_comma
      INTO lv_line.

    PERFORM json_add_line USING lv_line.

  ENDLOOP.

  PERFORM json_add_line USING '  }'.
  PERFORM json_add_line USING '}'.

ENDFORM.                    "build_function_json

*---------------------------------------------------------------------*
* Build class method JSON
*---------------------------------------------------------------------*
FORM build_method_json.

  DATA:
    ls_parameter TYPE ty_parameter,
    lv_count     TYPE i,
    lv_index     TYPE i,
    lv_line      TYPE string,
    lv_class     TYPE string,
    lv_method    TYPE string,
    lv_name      TYPE string,
    lv_direction TYPE string,
    lv_raw       TYPE string,
    lv_type      TYPE string,
    lv_required  TYPE string,
    lv_comma     TYPE c LENGTH 1.

  REFRESH gt_json.

  PERFORM json_escape
    USING gs_method-class_name
    CHANGING lv_class.

  PERFORM json_escape
    USING gs_method-method_name
    CHANGING lv_method.

  DESCRIBE TABLE gt_parameters LINES lv_count.

  PERFORM json_add_line USING '{'.

*---------------------------------------------------------------------*
* Class
*---------------------------------------------------------------------*
  CONCATENATE
    '  "class": "'
    p_class
    '",'
    INTO lv_line.

  PERFORM json_add_line USING lv_line.

*---------------------------------------------------------------------*
* Method
*---------------------------------------------------------------------*
  CONCATENATE
    '  "method": "'
    p_method
    '",'
    INTO lv_line.

  PERFORM json_add_line USING lv_line.

*---------------------------------------------------------------------*
* Parameters
*---------------------------------------------------------------------*
  PERFORM json_add_line USING '  "parameters": {'.

  CLEAR lv_index.

  LOOP AT gt_parameters INTO ls_parameter.

    ADD 1 TO lv_index.

    PERFORM json_escape
      USING ls_parameter-name
      CHANGING lv_name.

    PERFORM json_escape
      USING ls_parameter-direction
      CHANGING lv_direction.

    PERFORM json_escape
      USING ls_parameter-raw_direction
      CHANGING lv_raw.

    PERFORM json_escape
      USING ls_parameter-abap_type
      CHANGING lv_type.

    PERFORM get_json_boolean
      USING ls_parameter-required
      CHANGING lv_required.

    CONCATENATE
      '    "'
      lv_name
      '": {'
      INTO lv_line.

    PERFORM json_add_line USING lv_line.

    CONCATENATE
      '      "direction": "'
      lv_direction
      '",'
      INTO lv_line.

    PERFORM json_add_line USING lv_line.

    CONCATENATE
      '      "rawDirection": "'
      lv_raw
      '",'
      INTO lv_line.

    PERFORM json_add_line USING lv_line.

    CONCATENATE
      '      "abap_type": "'
      lv_type
      '",'
      INTO lv_line.

    PERFORM json_add_line USING lv_line.

    CONCATENATE
      '      "required": '
      lv_required
      INTO lv_line.

    PERFORM json_add_line USING lv_line.

    IF lv_index < lv_count.
      lv_comma = ','.
    ELSE.
      CLEAR lv_comma.
    ENDIF.

    CONCATENATE
      '    }'
      lv_comma
      INTO lv_line.

    PERFORM json_add_line USING lv_line.

  ENDLOOP.

  PERFORM json_add_line USING '  }'.
  PERFORM json_add_line USING '}'.

ENDFORM.                    "build_method_json

*---------------------------------------------------------------------*
* Convert ABAP flag to JSON boolean
*---------------------------------------------------------------------*
FORM get_json_boolean
  USING
    p_flag    TYPE c
  CHANGING
    p_boolean TYPE string.

  IF p_flag = 'X'.
    p_boolean = 'true'.
  ELSE.
    p_boolean = 'false'.
  ENDIF.

ENDFORM.                    "get_json_boolean

*---------------------------------------------------------------------*
* Add JSON line
*---------------------------------------------------------------------*
FORM json_add_line
  USING
    p_line.

  APPEND p_line TO gt_json.

ENDFORM.                    "json_add_line

*---------------------------------------------------------------------*
* Escape a JSON string
*---------------------------------------------------------------------*
FORM json_escape
  USING
    p_input  TYPE any
  CHANGING
    p_output TYPE string.

  p_output = p_input.

*---------------------------------------------------------------------*
* Escape backslash before escaping quotation marks
*---------------------------------------------------------------------*
  REPLACE ALL OCCURRENCES OF
    '\'
    IN p_output
    WITH '\\'.

  REPLACE ALL OCCURRENCES OF
    '"'
    IN p_output
    WITH '\"'.

  REPLACE ALL OCCURRENCES OF
    cl_abap_char_utilities=>cr_lf
    IN p_output
    WITH '\n'.

  REPLACE ALL OCCURRENCES OF
    cl_abap_char_utilities=>newline
    IN p_output
    WITH '\n'.

  REPLACE ALL OCCURRENCES OF
    cl_abap_char_utilities=>horizontal_tab
    IN p_output
    WITH '\t'.

ENDFORM.                    "json_escape

*---------------------------------------------------------------------*
* Optional frontend download
*---------------------------------------------------------------------*
FORM download_json.

  DATA: l_file          TYPE string,
        l_selected_file TYPE string,
        l_default_file  TYPE string,
        l_user_action   TYPE i.

  IF NOT r_table IS INITIAL.
    l_default_file = p_table.
  ELSEIF NOT r_func IS INITIAL.
    l_default_file = p_func.
  ELSE.
    l_default_file = |{ p_class }-{ p_method }|.
  ENDIF.

  CALL FUNCTION 'GUI_FILE_SAVE_DIALOG'
    EXPORTING
      window_title      = 'Choose filename'
      default_extension = 'json'
      default_file_name = l_default_file
      initial_directory = 'C:\temp'
    IMPORTING
      fullpath          = l_selected_file
      user_action       = l_user_action.

  IF l_selected_file IS INITIAL.
    MESSAGE s000 WITH 'Download cancelled'.
    EXIT.
  ENDIF.

  CALL FUNCTION 'GUI_DOWNLOAD'
    EXPORTING
      filename                = l_selected_file
      filetype                = 'ASC'
      confirm_overwrite       = 'X'
    TABLES
      data_tab                = gt_json
    EXCEPTIONS
      file_write_error        = 1
      no_batch                = 2
      gui_refuse_filetransfer = 3
      invalid_type            = 4
      no_authority            = 5
      unknown_error           = 6
      header_not_allowed      = 7
      separator_not_allowed   = 8
      filesize_not_allowed    = 9
      header_too_long         = 10
      dp_error_create         = 11
      dp_error_send           = 12
      dp_error_write          = 13
      unknown_dp_error        = 14
      access_denied           = 15
      dp_out_of_memory        = 16
      disk_full               = 17
      dp_timeout              = 18
      file_not_found          = 19
      dataprovider_exception  = 20
      control_flush_error     = 21
      OTHERS                  = 22.

  IF sy-subrc <> 0.
    MESSAGE 'Unable to download JSON file' TYPE 'E'.
  ENDIF.

ENDFORM.                    "download_json
*&---------------------------------------------------------------------*
*&      Form  READ_TABLE
*&---------------------------------------------------------------------*

FORM read_table .

  DATA: ls_field TYPE ty_ddic_field.

  CALL FUNCTION 'DDIF_FIELDINFO_GET'
    EXPORTING
      tabname        = p_table
    TABLES
      dfies_tab      = t_dfies
      fixed_values   = t_fixed_values
    EXCEPTIONS
      not_found      = 1
      internal_error = 2
      OTHERS         = 3.

  LOOP AT t_dfies INTO st_dfies.

    ls_field-name        = st_dfies-fieldname.
    ls_field-rollname    = st_dfies-rollname.
    ls_field-datatype    = st_dfies-datatype.
    ls_field-length      = st_dfies-intlen.                 "OUTPUTLEN
    ls_field-decimals    = st_dfies-decimals.
    ls_field-description = st_dfies-scrtext_l.
    ls_field-key_flag    = st_dfies-keyflag.

    APPEND ls_field TO gt_ddic_fields.

  ENDLOOP.

ENDFORM.                    " READ_TABLE
*&---------------------------------------------------------------------*
*&      Form  READ_FUNC
*&---------------------------------------------------------------------*

FORM read_func .

  DATA: ls_parameter TYPE ty_parameter.

  CALL FUNCTION 'RFC_GET_FUNCTION_INTERFACE'
    EXPORTING
      funcname                = p_func
      language                = sy-langu
      none_unicode_length     = ' '
    IMPORTING
      remote_basxml_supported = w_remote_basxml_supported
      remote_call             = w_remote_call
      update_task             = w_update_task
    TABLES
      params                  = t_params
      resumable_exceptions    = t_rsexc
    EXCEPTIONS
      fu_not_found            = 1
      nametab_fault           = 2
      OTHERS                  = 3.

  LOOP AT t_params INTO st_params.

    ls_parameter-name          = st_params-parameter.
    ls_parameter-raw_direction = st_params-paramclass.
    ls_parameter-abap_type     = st_params-tabname.
    ls_parameter-field_name    = st_params-fieldname.
    ls_parameter-required      = st_params-optional.

    CASE st_params-paramclass.
      WHEN 'E'.
        ls_parameter-direction = 'EXPORTING'.
      WHEN 'I'.
        ls_parameter-direction = 'IMPORTING'.
      WHEN 'T'.
        ls_parameter-direction = 'TABLES'.
    ENDCASE.

    APPEND ls_parameter TO gt_parameters.

  ENDLOOP.

ENDFORM.                    " READ_FUNC
*&---------------------------------------------------------------------*
*&      Form  READ_CLASS_METHOD
*&---------------------------------------------------------------------*

FORM read_class_method .

  DATA: ls_parameter TYPE ty_parameter.

  SELECT *
         INTO TABLE t_seosubcodf
         FROM seosubcodf
         WHERE clsname EQ p_class
           AND cmpname EQ p_method.

  LOOP AT t_seosubcodf INTO st_seosubcodf.

    ls_parameter-name          = st_seosubcodf-sconame.
    ls_parameter-raw_direction = st_seosubcodf-pardecltyp.
    ls_parameter-abap_type     = st_seosubcodf-type.
    "ls_parameter-field_name    = st_seosubcodf-
    ls_parameter-required      = st_seosubcodf-paroptionl.

    CASE st_seosubcodf-pardecltyp.
      WHEN '0'.
        ls_parameter-direction = 'IMPORTING'.
      WHEN '1'.
        ls_parameter-direction = 'EXPORTING'.
      WHEN '2'.
        ls_parameter-direction = 'CHANGING'.
      WHEN '3'.
        ls_parameter-direction = 'RETURNING'.
    ENDCASE.

    APPEND ls_parameter TO gt_parameters.

  ENDLOOP.

ENDFORM.                    " READ_CLASS_METHOD
*&---------------------------------------------------------------------*
*&      Form  OUTPUT_JSON
*&---------------------------------------------------------------------*

FORM output_json .

  DATA: lv_line TYPE string.

  LOOP AT gt_json INTO lv_line.

    WRITE:/ lv_line.

  ENDLOOP.

ENDFORM.                    " OUTPUT_JSON