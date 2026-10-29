local jsonc = require("luci.jsonc")
local storage = require("luci.function.NasStorage")

local ok, result = pcall(storage.getPoolInfo, {
    selector = {
        { value = { "internal_pool" } }
    }
})

if not ok then
    io.stderr:write(tostring(result))
    os.exit(2)
end

print(jsonc.stringify(result))
