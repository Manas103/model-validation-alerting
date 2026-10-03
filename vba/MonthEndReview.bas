Attribute VB_Name = "MonthEndReview"
' Month-end review workbook: recomputes the four no-arbitrage checks
' (put-call parity, strike monotonicity, butterfly convexity, calendar-spread)
' directly over the "Quotes" sheet, then reconciles its own output cell for
' cell against mvguard/surface_guardrails.py's results, pasted into the
' "EngineSummary" sheet by scripts/build_month_end_workbook.py.
'
' This machine has no Excel installed (no registered Excel.Application COM
' class), so this module is the real production design, not executed live.
' mvguard/vba_mirror.py is an independently written Python transliteration of
' the same algorithm, used to measure what this macro would produce; see that
' file's docstring. Tolerances below are the same four numbers in
' rules/option_surface_guardrails.yaml; this macro does not parse YAML, so a
' change there needs the same change made here by hand, which is the one
' honest cost of recomputing "in-sheet" instead of calling back into Python.

Option Explicit

Private Const PARITY_TOL As Double = 0.05
Private Const MONO_TOL As Double = 0.02
Private Const BUTTERFLY_TOL As Double = 0.02
Private Const CALENDAR_TOL As Double = 0.02

Private Const COL_SNAPSHOT As Long = 1
Private Const COL_SPOT As Long = 2
Private Const COL_RATE As Long = 3
Private Const COL_STRIKE As Long = 4
Private Const COL_MATURITY_DAYS As Long = 5
Private Const COL_MATURITY_YEARS As Long = 6
Private Const COL_CALL_MID As Long = 7
Private Const COL_PUT_MID As Long = 8

' One click: recompute every check, then reconcile against the Python engine.
Public Sub RunMonthEndReview()
    RefreshExceptionPack
    ReconcileToEngine
End Sub

' Reads "Quotes", writes every violation to "Exceptions" and a per-snapshot,
' per-family count to "VBASummary". Mirrors mvguard/surface_guardrails.py's
' four checks exactly, grouped by worksheet row instead of by Python dict.
Public Sub RefreshExceptionPack()
    Dim wsQuotes As Worksheet, wsExceptions As Worksheet, wsSummary As Worksheet
    Set wsQuotes = ThisWorkbook.Worksheets("Quotes")
    Set wsExceptions = ThisWorkbook.Worksheets("Exceptions")
    Set wsSummary = ThisWorkbook.Worksheets("VBASummary")

    wsExceptions.Cells.Clear
    wsExceptions.Range("A1:F1").Value = Array("SnapshotID", "Family", "Detail1", "Detail2", "Detail3", "Observed")

    Dim lastRow As Long
    lastRow = wsQuotes.Cells(wsQuotes.Rows.Count, COL_SNAPSHOT).End(xlUp).Row

    Dim counts As Object ' Scripting.Dictionary, key = SnapshotID & "|" & Family
    Set counts = CreateObject("Scripting.Dictionary")

    Dim outRow As Long
    outRow = 2

    Dim r As Long
    ' ---- Parity: one row at a time, no grouping needed ----
    For r = 2 To lastRow
        Dim spot As Double, rate As Double, strike As Double, matYears As Double
        Dim callMid As Double, putMid As Double, snap As String
        snap = wsQuotes.Cells(r, COL_SNAPSHOT).Value
        spot = wsQuotes.Cells(r, COL_SPOT).Value
        rate = wsQuotes.Cells(r, COL_RATE).Value
        strike = wsQuotes.Cells(r, COL_STRIKE).Value
        matYears = wsQuotes.Cells(r, COL_MATURITY_YEARS).Value
        callMid = wsQuotes.Cells(r, COL_CALL_MID).Value
        putMid = wsQuotes.Cells(r, COL_PUT_MID).Value

        Dim expected As Double, actual As Double, diff As Double
        expected = spot - strike * Exp(-rate * matYears)
        actual = callMid - putMid
        diff = actual - expected
        If Abs(diff) > PARITY_TOL Then
            wsExceptions.Cells(outRow, 1).Value = snap
            wsExceptions.Cells(outRow, 2).Value = "parity"
            wsExceptions.Cells(outRow, 3).Value = strike
            wsExceptions.Cells(outRow, 6).Value = diff
            outRow = outRow + 1
            BumpCount counts, snap, "parity"
        End If
    Next r

    ' ---- Monotonicity and butterfly: group by SnapshotID|MaturityDays ----
    Dim byMaturity As Object
    Set byMaturity = GroupRows(wsQuotes, lastRow, COL_MATURITY_DAYS)

    Dim keyM As Variant
    For Each keyM In byMaturity.Keys
        Dim groupM As Collection
        Set groupM = byMaturity(keyM)
        SortCollectionByColumn groupM, wsQuotes, COL_STRIKE

        Dim i As Long
        For i = 2 To groupM.Count
            Dim rowLo As Long, rowHi As Long
            rowLo = groupM(i - 1)
            rowHi = groupM(i)

            Dim callLo As Double, callHi As Double, putLo As Double, putHi As Double
            callLo = wsQuotes.Cells(rowLo, COL_CALL_MID).Value
            callHi = wsQuotes.Cells(rowHi, COL_CALL_MID).Value
            putLo = wsQuotes.Cells(rowLo, COL_PUT_MID).Value
            putHi = wsQuotes.Cells(rowHi, COL_PUT_MID).Value
            snap = wsQuotes.Cells(rowHi, COL_SNAPSHOT).Value

            If callHi > callLo + MONO_TOL Then
                wsExceptions.Cells(outRow, 1).Value = snap
                wsExceptions.Cells(outRow, 2).Value = "monotonicity"
                wsExceptions.Cells(outRow, 3).Value = "call"
                wsExceptions.Cells(outRow, 4).Value = wsQuotes.Cells(rowLo, COL_STRIKE).Value
                wsExceptions.Cells(outRow, 5).Value = wsQuotes.Cells(rowHi, COL_STRIKE).Value
                outRow = outRow + 1
                BumpCount counts, snap, "monotonicity"
            End If
            If putHi < putLo - MONO_TOL Then
                wsExceptions.Cells(outRow, 1).Value = snap
                wsExceptions.Cells(outRow, 2).Value = "monotonicity"
                wsExceptions.Cells(outRow, 3).Value = "put"
                wsExceptions.Cells(outRow, 4).Value = wsQuotes.Cells(rowLo, COL_STRIKE).Value
                wsExceptions.Cells(outRow, 5).Value = wsQuotes.Cells(rowHi, COL_STRIKE).Value
                outRow = outRow + 1
                BumpCount counts, snap, "monotonicity"
            End If
        Next i

        For i = 2 To groupM.Count - 1
            Dim row1 As Long, row2 As Long, row3 As Long
            row1 = groupM(i - 1): row2 = groupM(i): row3 = groupM(i + 1)
            Dim k1 As Double, k2 As Double, k3 As Double
            k1 = wsQuotes.Cells(row1, COL_STRIKE).Value
            k2 = wsQuotes.Cells(row2, COL_STRIKE).Value
            k3 = wsQuotes.Cells(row3, COL_STRIKE).Value
            If (k2 - k1) = (k3 - k2) Then
                Dim secondDiff As Double
                secondDiff = wsQuotes.Cells(row1, COL_CALL_MID).Value _
                    - 2 * wsQuotes.Cells(row2, COL_CALL_MID).Value _
                    + wsQuotes.Cells(row3, COL_CALL_MID).Value
                If secondDiff < -BUTTERFLY_TOL Then
                    snap = wsQuotes.Cells(row2, COL_SNAPSHOT).Value
                    wsExceptions.Cells(outRow, 1).Value = snap
                    wsExceptions.Cells(outRow, 2).Value = "butterfly"
                    wsExceptions.Cells(outRow, 3).Value = k1
                    wsExceptions.Cells(outRow, 4).Value = k2
                    wsExceptions.Cells(outRow, 5).Value = k3
                    wsExceptions.Cells(outRow, 6).Value = secondDiff
                    outRow = outRow + 1
                    BumpCount counts, snap, "butterfly"
                End If
            End If
        Next i
    Next keyM

    ' ---- Calendar: group by SnapshotID|Strike ----
    Dim byStrike As Object
    Set byStrike = GroupRows(wsQuotes, lastRow, COL_STRIKE)

    Dim keyS As Variant
    For Each keyS In byStrike.Keys
        Dim groupS As Collection
        Set groupS = byStrike(keyS)
        SortCollectionByColumn groupS, wsQuotes, COL_MATURITY_DAYS

        For i = 2 To groupS.Count
            Dim shortRow As Long, longRow As Long
            shortRow = groupS(i - 1)
            longRow = groupS(i)
            Dim callShort As Double, callLong As Double
            callShort = wsQuotes.Cells(shortRow, COL_CALL_MID).Value
            callLong = wsQuotes.Cells(longRow, COL_CALL_MID).Value
            If callLong < callShort - CALENDAR_TOL Then
                snap = wsQuotes.Cells(longRow, COL_SNAPSHOT).Value
                wsExceptions.Cells(outRow, 1).Value = snap
                wsExceptions.Cells(outRow, 2).Value = "calendar"
                wsExceptions.Cells(outRow, 3).Value = wsQuotes.Cells(shortRow, COL_STRIKE).Value
                wsExceptions.Cells(outRow, 4).Value = wsQuotes.Cells(shortRow, COL_MATURITY_DAYS).Value
                wsExceptions.Cells(outRow, 5).Value = wsQuotes.Cells(longRow, COL_MATURITY_DAYS).Value
                outRow = outRow + 1
                BumpCount counts, snap, "calendar"
            End If
        Next i
    Next keyS

    WriteSummary wsSummary, wsQuotes, lastRow, counts
End Sub

' Reads "VBASummary" (just written) and "EngineSummary" (pasted from the
' Python engine's real output), writes a MATCH/MISMATCH row per
' (SnapshotID, Family) into "Reconciliation", plus a final pass/fail cell.
Public Sub ReconcileToEngine()
    Dim wsVba As Worksheet, wsEngine As Worksheet, wsRecon As Worksheet
    Set wsVba = ThisWorkbook.Worksheets("VBASummary")
    Set wsEngine = ThisWorkbook.Worksheets("EngineSummary")
    Set wsRecon = ThisWorkbook.Worksheets("Reconciliation")

    wsRecon.Cells.Clear
    wsRecon.Range("A1:E1").Value = Array("SnapshotID", "Family", "EngineCount", "VBACount", "Match")

    Dim lastRow As Long
    lastRow = wsVba.Cells(wsVba.Rows.Count, 1).End(xlUp).Row

    Dim mismatches As Long
    mismatches = 0

    Dim r As Long, outRow As Long
    outRow = 2
    For r = 2 To lastRow
        Dim snap As String, fam As String
        Dim vbaCount As Long, engineCount As Long
        snap = wsVba.Cells(r, 1).Value
        fam = wsVba.Cells(r, 2).Value
        vbaCount = wsVba.Cells(r, 3).Value
        engineCount = FindEngineCount(wsEngine, snap, fam)

        Dim isMatch As Boolean
        isMatch = (vbaCount = engineCount)
        If Not isMatch Then mismatches = mismatches + 1

        wsRecon.Cells(outRow, 1).Value = snap
        wsRecon.Cells(outRow, 2).Value = fam
        wsRecon.Cells(outRow, 3).Value = engineCount
        wsRecon.Cells(outRow, 4).Value = vbaCount
        wsRecon.Cells(outRow, 5).Value = isMatch
        outRow = outRow + 1
    Next r

    wsRecon.Cells(outRow + 1, 1).Value = "Total mismatches"
    wsRecon.Cells(outRow + 1, 2).Value = mismatches
    wsRecon.Cells(outRow + 2, 1).Value = "ALL MATCH"
    wsRecon.Cells(outRow + 2, 2).Value = (mismatches = 0)
End Sub

' ---------------------------------------------------------------------
' Helpers
' ---------------------------------------------------------------------

Private Sub BumpCount(counts As Object, snap As String, fam As String)
    Dim k As String
    k = snap & "|" & fam
    If Not counts.Exists(k) Then
        counts.Add k, 0
    End If
    counts(k) = counts(k) + 1
End Sub

Private Function GroupRows(wsQuotes As Worksheet, lastRow As Long, groupCol As Long) As Object
    Dim groups As Object
    Set groups = CreateObject("Scripting.Dictionary")

    Dim r As Long
    For r = 2 To lastRow
        Dim snap As String, k As String
        snap = wsQuotes.Cells(r, COL_SNAPSHOT).Value
        k = snap & "|" & wsQuotes.Cells(r, groupCol).Value

        If Not groups.Exists(k) Then
            groups.Add k, New Collection
        End If
        groups(k).Add r
    Next r

    Set GroupRows = groups
End Function

' Insertion sort over a Collection of row numbers, keyed on one worksheet
' column. Collections have no native sort; groups here are at most 8 rows
' (strikes) or 5 rows (maturities), so O(n^2) is the right trade.
Private Sub SortCollectionByColumn(ByRef rows As Collection, wsQuotes As Worksheet, sortCol As Long)
    Dim n As Long
    n = rows.Count
    Dim i As Long, j As Long
    For i = 2 To n
        Dim currentRow As Long
        currentRow = rows(i)
        Dim currentKey As Double
        currentKey = wsQuotes.Cells(currentRow, sortCol).Value

        j = i - 1
        Do While j >= 1
            If wsQuotes.Cells(rows(j), sortCol).Value <= currentKey Then Exit Do
            j = j - 1
        Loop

        If j <> i - 1 Then
            rows.Add currentRow, before:=j + 1
            rows.Remove i
        End If
    Next i
End Sub

Private Sub WriteSummary(wsSummary As Worksheet, wsQuotes As Worksheet, lastRow As Long, counts As Object)
    wsSummary.Cells.Clear
    wsSummary.Range("A1:C1").Value = Array("SnapshotID", "Family", "Count")

    Dim snapshots As Object
    Set snapshots = CreateObject("Scripting.Dictionary")
    Dim r As Long
    For r = 2 To lastRow
        Dim snap As String
        snap = wsQuotes.Cells(r, COL_SNAPSHOT).Value
        If Not snapshots.Exists(snap) Then snapshots.Add snap, True
    Next r

    Dim families(3) As String
    families(0) = "parity": families(1) = "monotonicity"
    families(2) = "butterfly": families(3) = "calendar"

    Dim outRow As Long
    outRow = 2
    Dim snapKey As Variant, f As Long
    For Each snapKey In snapshots.Keys
        For f = 0 To 3
            Dim k As String
            k = CStr(snapKey) & "|" & families(f)
            Dim c As Long
            If counts.Exists(k) Then
                c = counts(k)
            Else
                c = 0
            End If
            wsSummary.Cells(outRow, 1).Value = snapKey
            wsSummary.Cells(outRow, 2).Value = families(f)
            wsSummary.Cells(outRow, 3).Value = c
            outRow = outRow + 1
        Next f
    Next snapKey
End Sub

Private Function FindEngineCount(wsEngine As Worksheet, snap As String, fam As String) As Long
    Dim lastRow As Long
    lastRow = wsEngine.Cells(wsEngine.Rows.Count, 1).End(xlUp).Row

    Dim r As Long
    For r = 2 To lastRow
        If wsEngine.Cells(r, 1).Value = snap And wsEngine.Cells(r, 2).Value = fam Then
            FindEngineCount = wsEngine.Cells(r, 3).Value
            Exit Function
        End If
    Next r
    FindEngineCount = -1 ' not found; ReconcileToEngine will report this as a mismatch
End Function
