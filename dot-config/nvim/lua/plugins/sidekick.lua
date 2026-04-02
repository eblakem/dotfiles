vim.pack.add({
        "https://github.com/folke/sidekick.nvim"
}, { confirm = false })

require("sidekick").setup({})

local vk = vim.keymap
vk.set("n", "<leader>aa", function() require("sidekick.cli").toggle({ name = "gemini", focus = true}) end , { silent = true, desc = "Gemini toggle" })


