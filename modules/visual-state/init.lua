function init(ctx)
  local visual = ctx.services.get("visual")
  local commands = ctx.services.get("commands")
  if visual == nil or commands == nil then
    ctx.log("visual-state: visual or command service unavailable")
    return
  end

  local function supported_kinds()
    return {"beliefs", "attributes", "activity", "memories", "recent", "mood", "sentiment", "stress", "summary"}
  end

  local function is_valid_kind(kind)
    for _, k in ipairs(supported_kinds()) do
      if k == kind then
        return true
      end
    end
    return false
  end

  commands:register("/visualize", function(args)
    local kind = "summary"
    if type(args) == "table" then
      kind = args[1] or "summary"
    end
    if not is_valid_kind(kind) then
      return "usage: /visualize [" .. table.concat(supported_kinds(), "|") .. "]"
    end
    local ok, result = pcall(function()
      return visual:build(kind)
    end)
    if not ok then
      return "visualize failed: " .. tostring(result)
    end
    local lines = {
      "visual state: " .. (result.kind or kind) .. "  (saved to " .. (result.path or "?") .. ")",
      "",
      result.text_chart or "",
      "",
      "caption: " .. (result.caption or ""),
    }
    return table.concat(lines, "\n")
  end)

  ctx.log("visual-state module loaded")
end
