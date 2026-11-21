-- 修改了 wb_runner.py / wb_core.py 之后运行：停止执行器、重新载入代码、再启动。不创建窗口。
(
    local wbDirectory = getFilenamePath (getSourceFileName())
    local wbRoot = pathConfig.appendPath wbDirectory ".."
    local wbConfig = pathConfig.appendPath wbRoot "config.json"
    fn wbPythonLiteral text =
    (
        "'" + (substituteString (substituteString text "\\" "/") "'" "\\'") + "'"
    )
    try
    (
        python.Execute ("import sys; p=" + wbPythonLiteral wbDirectory + "; sys.path.insert(0,p) if p not in sys.path else None")
        python.Execute "import importlib, wb_core, wb_runner\nwb_runner.stop()\nimportlib.reload(wb_core)\nimportlib.reload(wb_runner)"
        python.Execute ("import wb_runner; wb_runner.start(" + wbPythonLiteral wbConfig + ")")
    )
    catch
    (
        format "重载白底执行器失败：%\n" (getCurrentException())
    )
)
