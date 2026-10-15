param([Parameter(Mandatory)][string]$In, [Parameter(Mandatory)][string]$Out, [Parameter(Mandatory)][string]$Format)
# 한컴오피스 자동화로 한글 파일(HWP·HWPX)을 PDF로 저장한다(ADR 0023, scripts/v2_fetch_sources.py가 부른다).
# 창을 띄우지 않고, 성공하면 "ok"를 쓴다.
$hwp = New-Object -ComObject HWPFrame.HwpObject
try {
    $hwp.XHwpWindows.Item(0).Visible = $false
    if (-not $hwp.Open($In, $Format, "forceopen:true;versionwarning:false")) { throw "열기 실패: $In" }
    if (-not $hwp.SaveAs($Out, "PDF", "")) { throw "PDF 저장 실패: $Out" }
    "ok"
} finally {
    $hwp.Clear(1)
    $hwp.Quit()
}
