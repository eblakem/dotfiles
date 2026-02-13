-- lazy.nvim
return {
  'folke/snacks.nvim',
  ---@type snacks.Config
  opts = {
    image = {
      max_width = 60,
      max_height = 60,
      -- your image configuration comes here
      -- or leave it empty to use the default settings
      -- refer to the configuration section below
    },
  },
}
