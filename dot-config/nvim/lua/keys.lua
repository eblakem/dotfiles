local vk = vim.keymap
vk.set({ "n" }, "<Esc><Esc>", "<Esc>:nohlsearch<CR>", { silent = true })
vk.set({ "n" }, "<leader>sk", ":Telescope keymaps<CR>", { desc = "Keymaps" })
