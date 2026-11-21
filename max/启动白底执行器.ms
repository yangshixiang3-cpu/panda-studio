-- 拖入 Max 视口、通过“运行脚本”或以 -U MAXScript 启动。
(
    local wbSource = getSourceFileName()
    if wbSource == undefined or wbSource == "" or not (doesFileExist wbSource) do
        wbSource = systemTools.getEnvVariable "WHITEBG_RUNNER_SCRIPT"
    if wbSource == undefined or wbSource == "" or not (doesFileExist wbSource) do
        throw "无法定位启动白底执行器.ms。"
    local wbDirectory = getFilenamePath wbSource
    local wbRoot = pathConfig.appendPath wbDirectory ".."
    local wbConfig = pathConfig.appendPath wbRoot "config.json"
    fn wbPythonLiteral text =
    (
        "'" + (substituteString (substituteString text "\\" "/") "'" "\\'") + "'"
    )
    try
    (
        python.Execute ("import sys; p=" + wbPythonLiteral wbDirectory + "; sys.path.insert(0,p) if p not in sys.path else None")
        python.ExecuteFile (pathConfig.appendPath wbDirectory "wb_runner.py")
        python.Execute ("import wb_runner; wb_runner.start(" + wbPythonLiteral wbConfig + ")")
    )
    catch
    (
        local wbError = "启动白底执行器脚本失败：" + (getCurrentException())
        try
        (
            python.Execute ("import wb_core; wb_core.append_runner_log(" +
                wbPythonLiteral (pathConfig.appendPath wbRoot "exchange") + ", " + wbPythonLiteral wbError + ")")
        )
        catch (format "白底执行器无法写入启动错误日志：%\n" wbError)
    )
)
