set script_dir [file dirname [info script]]
set parent_dir [file normalize "$script_dir/.."]
set main_dir [file normalize "$script_dir/../.."]
puts "reading lib.."
foreach libFile [glob "$parent_dir/ASAP7/LIB/*nldm*.lib"] {
    puts "lib: $libFile"
    read_liberty $libFile
}
puts "reading lef.."
read_lef $parent_dir/ASAP7/techlef/asap7_tech_1x_201209.lef
foreach lef [glob "$parent_dir/ASAP7/LEF/*.lef"] {
    read_lef $lef
}
puts "reading def.."
read_def $parent_dir/aes_cipher_top/aes_cipher_top.def
read_sdc $parent_dir/aes_cipher_top/aes_cipher_top.sdc
source $parent_dir/ASAP7/setRC.tcl
estimate_parasitics -placement