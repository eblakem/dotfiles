function gem --wraps="tgpt" --description "ask gemini questions"
    tgpt --provider gemini --key $GEMAPI $argv
end
