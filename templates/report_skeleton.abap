Use this exact content for templates/report_skeleton.abap:

REPORT z_report    LINE-SIZE 132
                   LINE-COUNT 65
                   MESSAGE-ID 38
                   NO STANDARD PAGE HEADING.
************************************************************************
*  Report      : Z_REPORT                Author :                      *
*                                                                      *
*  Log Number  : XXXX                    Date   : sy-datum             *
*                                                                      *
*  Description :                                                       *
*  Program description goes here                                       *
*                                                                      *
************************************************************************
*  Revision History                                                    *
************************************************************************
*  Date        :                          Mod ID      :                *
*                                                                      *
*  Name        :                          Log Number  :                *
*                                                                      *
*  Description :                                                       *
*                                                                      *
************************************************************************
*  DATA SECTION
************************************************************************

* Includes

* Table declarations

* Types

* Internal tables

* Structures and work areas

* Constants

* Data

* Ranges

* Selection screen
SELECTION-SCREEN BEGIN OF BLOCK b1 WITH FRAME TITLE text-s01.

SELECTION-SCREEN END OF BLOCK b1.

* Field groups

* Field symbols

************************************************************************
* LOGIC SECTION
************************************************************************

* Initialisation

* Selection-screen checks

*---------------------------------------------------------------------*
* Start of selection
*---------------------------------------------------------------------*
START-OF-SELECTION.

  PERFORM <read_data_form>.
  PERFORM <process_data_form>.
  PERFORM <output_data_form>.

END-OF-SELECTION.

*---------------------------------------------------------------------*
* Form routines
*---------------------------------------------------------------------*

FORM <read_data_form>.
ENDFORM.

FORM <process_data_form>.
ENDFORM.

FORM <output_data_form>.
ENDFORM.