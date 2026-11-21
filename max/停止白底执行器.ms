-- 停止后续轮询；同步任务会先完成清理，不强行中断渲染。
(
    local wbDirectory = getFilenamePath (getSourceFileName())
    fn wbPythonLiteral text =
    (
        "'" + (substituteString (substituteString text "\\" "/") "'" "\\'") + "'"
    )
    try
    (
        python.Execute ("import sys; p=" + wbPythonLiteral wbDirectory + "; sys.path.insert(0,p) if p not in sys.path else None")
        python.Execute "import wb_runner; wb_runner.stop()"
    )
    catch
    (
        local wbError = "停止白底执行器脚本失败：" + (getCurrentException())
        try
        (
            python.Execute ("import wb_core; wb_core.append_runner_log(" +
                wbPythonLiteral (pathConfig.appendPath wbDirectory "../exchange") + ", " + wbPythonLiteral wbError + ")")
        )
        catch (format "白底执行器无法写入停止错误日志：%\n" wbError)
    )
)
