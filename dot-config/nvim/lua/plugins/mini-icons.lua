vim.pack.add({
	"https://github.com/nvim-mini/mini.icons",
}, { confirm = false })

local icons = require("mini.icons")
icons.setup({})
icons.mock_nvim_web_devicons()
