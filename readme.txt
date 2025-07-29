如果要用local的openroad
openroad -python ./src/no2model.py --design ./ICCAD25_PorbC --wl 1 --power 1 --timing 1

如果要用我編譯好的openroad(要在container底下)
./openroad/bin/openroad -python ./src/no2model.py --design ./ICCAD25_PorbC --wl 1 --power 1 --timing 1